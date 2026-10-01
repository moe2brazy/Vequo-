/**
 * AI 快答共享实现 —— 悬浮 AI 客服（AiAssistantFab）与消息中心「AI 管理员」（chat store）共用。
 *
 * 与 FAQ（src/assistantFaq.ts）配合：
 *  - FAQ 关键词命中：由调用方本地秒回；
 *  - 未命中：走本模块调用 /api/agent/stream 单轮问答（不携带 history，无记忆、不写对话存储）。
 */
export interface AgentAnswer {
  text: string
  type?: string
}

const STEP_LABELS: Record<string, string> = {
  '意图理解': '正在理解意图…',
  '表匹配': '正在匹配数据表…',
  '读取表结构': '正在读取表结构…',
  'SQL生成': '正在生成 SQL…',
  'SQL执行': '正在执行查询…',
  'SQL重试': '正在修复 SQL…',
  '兜底SQL': '正在使用备选查询…',
  '结果复核': '正在复核结果…',
  '图表生成': '正在生成图表…',
  '记忆检索': '正在检索历史记忆…',
  '思考中': '正在思考…',
  'ML建模': '正在准备建模…',
  'ML方案设计': '正在设计建模方案…',
  '意图分类': '正在分类意图…',
}

export async function askAgentStream(
  query: string,
  opts: { onStep?: (label: string) => void; signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<AgentAnswer> {
  // 2026-10-01 修复：
  // 1) 增加整体超时（默认 120s）——后端 hang 时此前气泡会永远停在"正在思考…"
  // 2) 内部 AbortController 与外部 signal 级联，finally 中 reader.cancel()
  //    ——此前中途抛错时 SSE 连接不关闭，长会话会耗尽浏览器同域连接数
  const timeoutMs = opts.timeoutMs ?? 120_000
  const ctrl = new AbortController()
  const onOuterAbort = () => ctrl.abort()
  if (opts.signal) {
    if (opts.signal.aborted) ctrl.abort()
    else opts.signal.addEventListener('abort', onOuterAbort, { once: true })
  }
  const timer = timeoutMs > 0 ? setTimeout(() => ctrl.abort(new Error('请求超时')), timeoutMs) : null
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null
  try {
    const resp = await fetch('/api/agent/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
      signal: ctrl.signal,
    })
    if (!resp.ok || !resp.body) {
      const err = await resp.json().catch(() => null)
      throw new Error(err?.detail || `请求失败(${resp.status})`)
    }
    reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    let final: any = null

    const handlePayload = (payload: any) => {
      if (!payload || typeof payload.type !== 'string') return
      if (payload.type === 'step') {
        opts.onStep?.(STEP_LABELS[payload.name] || `正在${payload.name || '分析'}…`)
      } else if (payload.type === 'done') {
        final = payload.response || {}
      } else if (payload.type === 'error') {
        throw new Error(payload.message || payload.content || 'Agent 执行失败')
      }
    }

    // 与主循环一致的事件切分：按空行分块、事件内多行 data: 拼为同一 JSON
    const processEvents = (text: string) => {
      const events = text.split('\n\n')
      const rest = events.pop() || ''
      for (const evt of events) {
        const dataLines: string[] = []
        for (const line of evt.split('\n')) {
          if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
        }
        if (!dataLines.length) continue
        try {
          handlePayload(JSON.parse(dataLines.join('\n')))
        } catch (e) {
          if (e instanceof SyntaxError) continue // 畸形事件跳过，不中断整条流
          throw e
        }
      }
      return rest
    }

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // 归一化换行，兼容 \r\n\r\n 分隔
      buffer = processEvents(buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n'))
    }
    // flush 解码器尾部缓冲（最后一段无空行结尾的完整事件）——
    // 2026-10-01 修复：此前尾部逐行 JSON.parse，多行 data: 事件会被静默丢弃（最终回答丢失）
    buffer += decoder.decode()
    buffer = processEvents(buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n') + '\n\n')

    const type: string = final?.type || ''
    let answer: string = String(final?.answer || '').trim()
    if (type === 'data_query') {
      const rows = final?.result?.rows?.length ?? 0
      answer = answer || `已查询到 ${rows} 条数据`
      answer += '\n\n（数据查询的完整表格与图表请前往「智能问析」查看）'
    }
    if (!answer) {
      answer = '抱歉，我没有理解这个问题，可以换个说法再问一次，或前往「智能问析」使用完整 AI 分析。'
    }
    return { text: answer, type }
  } finally {
    if (timer) clearTimeout(timer)
    if (opts.signal) opts.signal.removeEventListener('abort', onOuterAbort)
    try { await reader?.cancel() } catch { /* 流已结束/已出错，忽略 */ }
  }
}
