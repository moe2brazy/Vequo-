<template>
  <div class="register-page">
    <OpsWavyField />

    <header class="register-nav">
      <button class="register-brand" aria-label="返回登录" @click="emit('cancel')">
        <b aria-hidden="true"></b><span>Vequo 维阔</span><small>CREATE ACCOUNT</small>
      </button>
      <button class="register-back" @click="emit('cancel')">← 返回登录</button>
    </header>

    <main class="register-main">
      <section class="register-intro">
        <p class="register-code"><span>00</span> CREATE ACCOUNT</p>
        <h1>先注册一个<br><em>属于你的账号。</em></h1>
        <p class="register-summary">填写邮箱并获取验证码，设置密码后创建账号；注册成功后回到登录页登录进入工作台。新账号默认为「待授权」，管理员授权后才有数据权限。</p>

        <div class="register-progress" aria-label="填写进度">
          <div><strong>{{ String(completedCount).padStart(2, '0') }}</strong><span>/ {{ String(fields.length).padStart(2, '0') }}</span></div>
          <i><b :style="{ transform: `scaleX(${completedCount / fields.length})` }"></b></i>
          <small>{{ allComplete ? '信息已填写完整' : (completedCount ? '信息正在填写' : '请从邮箱开始填写') }}</small>
        </div>

        <div class="privacy-note"><i></i><span>DEFAULT PENDING</span><b>注册后需管理员授权</b></div>
      </section>

      <section class="register-panel">
        <div class="panel-heading"><span>CREATE / ACCOUNT</span><small>填写全部四项后注册</small></div>
        <form @submit.prevent="submitProfile">
          <div class="focus-rail" aria-hidden="true">
            <i></i>
            <span class="focus-pointer" :style="{ transform: `translate3d(0, ${activeIndex * 92}px, 0)` }"><b>→</b></span>
          </div>

          <label v-for="(field, index) in fields" :key="field.key" class="form-row" :class="{ 'is-active': activeIndex === index, 'is-complete': isComplete(field.key), 'is-missing': attemptedSubmit && !isComplete(field.key) }">
            <span class="row-index">0{{ index + 1 }}</span>
            <span class="row-copy"><small>{{ field.en }}</small><b>{{ field.label }}</b></span>
            <span class="row-input">
              <input v-model="form[field.key]" :type="field.type" :placeholder="field.placeholder" autocomplete="off" @focus="activeIndex = index" @input="activeIndex = index; attemptedSubmit = false; registerError = ''">
              <button v-if="field.key === 'code'" type="button" class="code-btn" :disabled="sendingCode || codeCooldown > 0" @click="handleSendCode">{{ sendingCode ? '发送中…' : (codeCooldown > 0 ? `重新发送（${codeCooldown}s）` : '获取验证码') }}</button>
            </span>
            <svg class="row-check" viewBox="0 0 54 54" aria-hidden="true">
              <rect x="5" y="5" width="44" height="44" rx="14" />
              <path d="m15 27 8 9 17-20" />
            </svg>
          </label>

          <p v-if="registerError" class="register-error" role="alert">{{ registerError }}</p>
          <div class="form-actions">
            <p :class="{ 'has-error': attemptedSubmit && !allComplete }"><span>*</span> {{ attemptedSubmit && !allComplete ? `还有 ${fields.length - completedCount} 项未填写，请补全后再注册` : '所有字段均为必填，两次密码须一致。' }}</p>
            <button type="submit" :disabled="submitting"><span>{{ submitting ? '正在创建账号' : '注册并去登录' }}</span><b>↗</b></button>
          </div>
        </form>
      </section>
    </main>

    <Transition name="success-pop">
      <div v-if="saved" class="register-success" role="status" aria-live="polite">
        <svg viewBox="0 0 88 88" aria-hidden="true"><circle cx="44" cy="44" r="39"/><path d="m25 44 12 13 27-30"/></svg>
        <small>ACCOUNT / CREATED</small>
        <strong>注册成功</strong>
        <span>正在跳转到登录</span>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import OpsWavyField from './OpsWavyField.vue'
import { register, sendEmailCode, logout } from '../auth'

type FieldKey = 'email' | 'code' | 'password' | 'confirm'

const emit = defineEmits<{ complete: [email: string]; cancel: [] }>()
const fields: { key: FieldKey; label: string; en: string; placeholder: string; type: string }[] = [
  { key: 'email', label: '邮箱', en: 'EMAIL', placeholder: '用于接收验证码并登录', type: 'text' },
  { key: 'code', label: '验证码', en: 'VERIFY CODE', placeholder: '邮件中的 6 位验证码', type: 'text' },
  { key: 'password', label: '密码', en: 'PASSWORD', placeholder: '至少 6 位', type: 'password' },
  { key: 'confirm', label: '确认密码', en: 'CONFIRM PASSWORD', placeholder: '请再次输入密码', type: 'password' },
]
const form = reactive<Record<FieldKey, string>>({ email: '', code: '', password: '', confirm: '' })
const activeIndex = ref(0)
const submitting = ref(false)
const saved = ref(false)
const attemptedSubmit = ref(false)  // 尝试提交但未填满 → 高亮未填项
const registerError = ref('')       // 注册失败（如验证码错误/邮箱已注册）时的错误提示
const sendingCode = ref(false)      // 正在发送验证码
const codeCooldown = ref(0)         // 重发倒计时（秒）
let completeTimer = 0
let codeTimer = 0

const completedCount = computed(() => fields.filter(field => isComplete(field.key)).length)
const allComplete = computed(() => fields.every(field => isComplete(field.key)))
function isComplete(key: FieldKey) { return form[key].trim().length > 0 }

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const EMAIL_HINT = '正确格式示例：name@example.com'

/** 邮箱格式体检：返回可直接展示给普通用户的中文原因，空串表示通过。
 *  与后端 email_codes.email_format_error 保持一致（本地先拦一道，省一次网络往返），
 *  逐条说明具体错在哪，避免用户面对笼统的「格式不正确」反复试错。 */
function emailFormatHint(email: string): string {
  const raw = email.trim()
  if (!raw) return '请填写邮箱地址'
  // 中文输入法下极易打出全角 ＠／．／空格，肉眼几乎看不出
  if (/[^\x00-\x7F]/.test(raw)) return '邮箱地址里有中文或全角字符，请切换到英文输入法重新输入'
  if (/\s/.test(raw)) return '邮箱地址中间不能有空格'
  if (!raw.includes('@')) return `邮箱地址缺少 @ 符号（${EMAIL_HINT}）`
  if (raw.split('@').length > 2) return '邮箱地址里出现了多个 @ 符号'
  const [local = '', domain = ''] = raw.split('@')
  if (!local) return `@ 前面缺少邮箱名称（${EMAIL_HINT}）`
  if (!domain) return `@ 后面缺少邮箱域名（${EMAIL_HINT}）`
  if (!domain.includes('.')) return `邮箱域名缺少后缀，如 @qq.com 里的 .com（${EMAIL_HINT}）`
  if (/\.$|^\.|\.\./.test(domain)) return '邮箱域名的点号位置不对，请检查域名部分'
  if (/\.$|^\.|\.\./.test(local)) return '邮箱名称不能以点号开头或结尾，也不能出现连续两个点号'
  if (domain.split('.').some(part => !part || part.startsWith('-') || part.endsWith('-'))) return '邮箱域名的连字符位置不对，请检查域名部分'
  if (!EMAIL_RE.test(raw)) return `邮箱格式不正确（${EMAIL_HINT}）`
  return ''
}

/** 表单校验：返回错误文案，空串表示通过。
 *  邮箱/验证码/密码规则与后端保持一致，避免「前端放过、后端报错」。 */
function validateForm(): string {
  const emailHint = emailFormatHint(form.email)
  if (emailHint) return emailHint
  if (!form.code.trim()) return '请填写验证码'
  if (!/^\d{6}$/.test(form.code.trim())) return '验证码为 6 位数字'
  if (form.password.length < 6) return '密码至少 6 位'
  if (form.password !== form.confirm) return '两次输入的密码不一致'
  return ''
}

/** 获取邮箱验证码：先校验邮箱格式，成功后进入重发倒计时 */
async function handleSendCode() {
  if (sendingCode.value || codeCooldown.value > 0) return
  const email = form.email.trim()
  const emailHint = emailFormatHint(email)
  if (emailHint) {
    registerError.value = emailHint
    attemptedSubmit.value = true
    activeIndex.value = 0
    return
  }
  sendingCode.value = true
  registerError.value = ''
  try {
    const res = await sendEmailCode(email)
    codeCooldown.value = res.cooldown
    // 后端没把邮件真发出去时必须当场说清：否则界面显示「已发送」，
    // 用户只能一直等一封永远不会来的邮件——本次故障就是这样被藏住的。
    if (!res.sent) {
      registerError.value = `验证码已生成，但邮件没有发出：${res.hint || '后端邮件服务未配置'}。`
        + '请到后端控制台查看本次验证码，或补全 backend/.env 的 MAIL_SMTP_* 配置后重启后端。'
    }
    window.clearInterval(codeTimer)
    codeTimer = window.setInterval(() => {
      codeCooldown.value -= 1
      if (codeCooldown.value <= 0) window.clearInterval(codeTimer)
    }, 1000)
  } catch (e: any) {
    registerError.value = e?.message || '验证码发送失败，请稍后重试'
  } finally {
    sendingCode.value = false
  }
}

async function submitProfile() {
  if (submitting.value) return
  if (!allComplete.value) {
    attemptedSubmit.value = true  // 全部字段必填：未填满则高亮缺失项，不提交
    return
  }
  const invalid = validateForm()
  if (invalid) {
    registerError.value = invalid
    attemptedSubmit.value = true
    return
  }
  submitting.value = true
  registerError.value = ''
  try {
    const email = form.email.trim().toLowerCase()
    const displayName = email.split('@')[0]
    // 注册后端真实账户（默认「待授权」角色，管理员授权前无数据权限）。
    await register(email, form.code.trim(), form.password, displayName)
    // 后端注册接口会顺带签发 token（原「注册即登录」设计），这里立即静默登出丢弃该登录态：
    // 注册成功后回登录页手动登录，不直接进工作台。silent=true 不派发 auth:logout，
    // 避免登出事件把视图踢回初始页、打断本页的跳转流程。
    logout(true)
    form.password = ''
    form.confirm = ''
    saved.value = true
    completeTimer = window.setTimeout(() => emit('complete', email), window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 180 : 1050)
  } catch (e: any) {
    registerError.value = e?.message || '注册失败，请重试'
    submitting.value = false
  }
}

onBeforeUnmount(() => { window.clearTimeout(completeTimer); window.clearInterval(codeTimer) })
</script>

<style scoped>
.register-page{position:relative;isolation:isolate;min-height:100svh;overflow:hidden;color:#1d2129;background:#f7f8fa;font-family:Arial,"Noto Sans CJK SC",sans-serif}.register-page:before{position:absolute;z-index:0;inset:0;content:'';pointer-events:none;background:radial-gradient(circle at 12% 82%,rgba(255,119,94,.16),transparent 30%),radial-gradient(circle at 78% 15%,rgba(77, 158, 255,.42),transparent 31%)}button{color:inherit}.register-nav{position:relative;z-index:5;display:flex;align-items:center;justify-content:space-between;padding:24px clamp(22px,4.5vw,72px);border-bottom:1px solid rgba(29, 33, 41,.16)}.register-brand{display:flex;min-height:44px;align-items:center;gap:10px;border:0;background:transparent}.register-brand b{display:block;width:46px;height:46px;background:url('/brand/vequo-app-icon.svg') center/contain no-repeat}.register-brand span{font:700 20px Rajdhani,sans-serif;letter-spacing:.18em}.register-brand small{padding-left:10px;border-left:1px solid rgba(29, 33, 41,.22);font:600 8px Rajdhani,sans-serif;letter-spacing:.15em;opacity:.58}.register-back{min-height:44px;padding:0 18px;border:1px solid #1d2129;border-radius:24px;background:rgba(243,241,232,.55);font-size:12px;backdrop-filter:blur(10px)}.register-back:hover{color:#f7f8fa;background:#1d2129}
.register-main{position:relative;z-index:2;display:grid;grid-template-columns:minmax(360px,.82fr) minmax(560px,1.18fr);gap:7vw;min-height:calc(100svh - 87px);align-items:center;padding:55px clamp(28px,6vw,96px) 64px}.register-code{display:flex;align-items:center;gap:12px;margin:0 0 26px;font:600 9px Rajdhani,sans-serif;letter-spacing:.18em}.register-code span{display:grid;width:27px;height:27px;place-items:center;border:1px solid currentColor;border-radius:50%;letter-spacing:0}.register-intro h1{margin:0;font-size:clamp(56px,6.2vw,104px);font-weight:400;line-height:.91;letter-spacing:-.065em}.register-intro h1 em{color:#1677ff;font-family:Georgia,"Songti SC",serif;font-weight:400}.register-summary{max-width:570px;margin:31px 0 0;color:#4e5969;font-size:14px;line-height:1.9}.register-progress{max-width:430px;margin-top:49px;padding-top:20px;border-top:1px solid rgba(29, 33, 41,.2)}.register-progress>div{display:flex;align-items:baseline}.register-progress strong{font:500 34px Rajdhani,sans-serif}.register-progress span{margin-left:5px;color:#86909c;font:600 12px Rajdhani,sans-serif}.register-progress>i{display:block;height:2px;margin:12px 0;background:rgba(29, 33, 41,.13)}.register-progress>i b{display:block;width:100%;height:100%;background:#4D9EFF;transform-origin:left;transition:transform .45s cubic-bezier(.22,.8,.2,1)}.register-progress small{color:#4e5969;font-size:10px}.privacy-note{display:grid;grid-template-columns:auto 1fr;gap:3px 9px;align-items:center;width:max-content;margin-top:36px;padding:13px 16px;border:1px solid rgba(29, 33, 41,.2);border-radius:15px;background:rgba(255,255,255,.35);backdrop-filter:blur(12px)}.privacy-note i{grid-row:1/3;width:8px;height:8px;border-radius:50%;background:#4D9EFF;box-shadow:0 0 0 5px rgba(77, 158, 255,.14)}.privacy-note span{font:600 7px Rajdhani,sans-serif;letter-spacing:.16em}.privacy-note b{font-size:11px;font-weight:500}
.register-panel{position:relative;padding:clamp(26px,3.5vw,54px);border:1px solid rgba(29, 33, 41,.23);border-radius:32px;background:rgba(247, 248, 250,.66);box-shadow:0 35px 90px rgba(29, 33, 41,.1);backdrop-filter:blur(18px)}.panel-heading{display:flex;justify-content:space-between;margin-bottom:23px;padding-bottom:17px;border-bottom:1px solid rgba(29, 33, 41,.14)}.panel-heading span,.panel-heading small{font:600 9px Rajdhani,sans-serif;letter-spacing:.16em}.panel-heading small{color:#4e5969;letter-spacing:.06em}.register-panel form{position:relative}.form-row{position:relative;display:grid;grid-template-columns:32px minmax(120px,.55fr) minmax(190px,1fr) 54px;gap:14px;align-items:center;min-height:92px;border-bottom:1px solid rgba(29, 33, 41,.14);cursor:text}.row-index{font:600 9px Rajdhani,sans-serif;opacity:.48}.row-copy{display:grid;gap:5px}.row-copy small{font:600 7px Rajdhani,sans-serif;letter-spacing:.16em;color:#4e5969}.row-copy b{font-size:13px;font-weight:500}.form-row input{width:100%;height:48px;padding:0 16px;border:1px solid transparent;border-radius:15px;outline:0;color:#1d2129;background:rgba(255,255,255,.42);font-size:13px;transition:border-color .25s,background .25s,transform .25s}
.form-row .row-input{display:flex;align-items:center;gap:8px;grid-column:3}.form-row .row-input input{flex:1;min-width:0}.code-btn{flex:0 0 auto;min-height:36px;padding:0 13px;border:1px solid #4D9EFF;border-radius:18px;background:#4D9EFF;color:#ffffff;font-size:12px;cursor:pointer;white-space:nowrap}.code-btn:hover:not(:disabled){border-color:#1d2129;background:#1d2129}.code-btn:disabled{cursor:wait;opacity:.55}.form-row input::placeholder{color:#a9aeb8}.form-row.is-active input{border-color:#1677ff;background:#fff;transform:translateX(4px)}.form-row.is-missing input{border-color:#d35f50;background:#fff}.row-check{width:45px;height:45px;overflow:visible}.row-check rect,.row-check path{fill:none;stroke:#1d2129;vector-effect:non-scaling-stroke}.row-check rect{stroke-width:1;opacity:.28}.row-check path{stroke:#1677ff;stroke-width:4;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:45;stroke-dashoffset:45;transition:stroke-dashoffset .5s cubic-bezier(.22,.8,.2,1)}.form-row.is-complete .row-check rect{fill:#4D9EFF;stroke:#1d2129;opacity:1}.form-row.is-complete .row-check path{stroke-dashoffset:0}.focus-rail{position:absolute;z-index:3;top:0;right:-34px;width:22px;height:368px;pointer-events:none}.focus-rail>i{position:absolute;top:18px;bottom:18px;left:10px;width:1px;background:rgba(29, 33, 41,.14)}.focus-pointer{position:absolute;top:34px;left:0;display:grid;width:22px;height:22px;place-items:center;border-radius:50%;color:#ffffff;background:#4D9EFF;box-shadow:0 0 0 6px rgba(77, 158, 255,.26);transition:transform .42s cubic-bezier(.22,.8,.2,1)}.focus-pointer b{font-size:12px}.register-error{margin:18px 0 0;padding:10px 14px;border:1px solid rgba(211,95,80,.35);border-radius:12px;color:#c04030;background:rgba(211,95,80,.08);font-size:11px;line-height:1.5}.form-actions{display:flex;align-items:center;justify-content:space-between;gap:25px;margin-top:28px}.form-actions p{max-width:260px;margin:0;color:#4e5969;font-size:10px;line-height:1.6}.form-actions p span{color:#d35f50}.form-actions p.has-error{color:#c04030;font-weight:500}.form-actions button{display:flex;min-width:215px;min-height:54px;align-items:center;justify-content:space-between;padding:0 20px;border:1px solid #4D9EFF;border-radius:28px;background:#4D9EFF;color:#ffffff;font-size:12px}.form-actions button:hover{color:#f7f8fa;background:#1d2129}.form-actions button:disabled{cursor:wait}
.register-success{position:fixed;z-index:30;inset:0;display:grid;place-content:center;justify-items:center;color:#1d2129;background:rgba(77, 158, 255,.93);backdrop-filter:blur(18px)}.register-success svg{width:90px;overflow:visible}.register-success circle,.register-success path{fill:none;stroke:#1d2129}.register-success circle{stroke-width:1}.register-success path{stroke-width:5;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:70;animation:success-check .65s ease both}.register-success small{margin-top:26px;font:600 9px Rajdhani,sans-serif;letter-spacing:.18em}.register-success strong{margin:8px 0;font:400 42px Georgia,"Songti SC",serif}.register-success span{font-size:12px}.success-pop-enter-active,.success-pop-leave-active{transition:opacity .35s,transform .5s cubic-bezier(.22,.8,.2,1)}.success-pop-enter-from,.success-pop-leave-to{opacity:0;transform:translateY(30px)}@keyframes success-check{from{stroke-dashoffset:70}to{stroke-dashoffset:0}}
@media(max-width:1000px){.register-main{grid-template-columns:1fr;gap:55px;padding-top:75px}.register-intro h1{font-size:76px}.register-panel{max-width:780px;width:100%}}
@media(max-width:680px){.register-page{overflow:auto}.register-nav{padding:17px}.register-brand small{display:none}.register-main{display:block;padding:60px 18px 80px}.register-intro h1{font-size:clamp(55px,17vw,76px)}.register-summary{font-size:13px}.register-panel{margin-top:55px;padding:22px 17px;border-radius:23px}.panel-heading small{display:none}.form-row{grid-template-columns:25px 1fr 45px;min-height:110px;padding:14px 0}.row-copy{grid-column:2}.form-row .row-input{grid-column:2;grid-row:2;align-items:stretch}.form-row .row-input input{flex:1}.row-check{grid-column:3;grid-row:1/3}.focus-rail{display:none}.form-actions{align-items:stretch;flex-direction:column}.form-actions button{width:100%}.register-success strong{font-size:38px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
</style>
