<template>
  <div v-if="loading" class="text-center text-gray-400 py-8 text-sm">加载中...</div>
  <div v-else-if="!tables.length" class="text-center text-gray-400 py-8 text-sm">暂无数据表</div>
  <div v-else class="grid gap-3 md:grid-cols-2">
    <!-- ER 风格表卡片：默认全部展开，完整展示所有字段 -->
    <div
      v-for="tbl in tables"
      :key="tbl.table_name"
      class="border border-gray-200 rounded-xl overflow-hidden bg-white shadow-sm hover:shadow-md transition"
    >
      <!-- 卡片头 -->
      <div class="flex items-center gap-2 px-4 py-2.5 bg-gradient-to-r from-slate-50 to-white border-b border-gray-100">
        <span class="text-lg"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <line x1="8" x2="21" y1="6" y2="6" /> <line x1="8" x2="21" y1="12" y2="12" /> <line x1="8" x2="21" y1="18" y2="18" /> <line x1="3" x2="3.01" y1="6" y2="6" /> <line x1="3" x2="3.01" y1="12" y2="12" /> <line x1="3" x2="3.01" y1="18" y2="18" /> </svg></span></span>
        <div class="min-w-0 flex-1">
          <div class="text-sm font-semibold text-gray-800 truncate">
            {{ tbl.chinese_name || tbl.table_name }}
            <span v-if="tbl.chinese_name" class="text-xs text-gray-400 font-normal">（{{ tbl.table_name }}）</span>
          </div>
          <div class="text-[10px] text-gray-400 mt-0.5">
            {{ tbl.columns.length }} 个字段 · {{ (tbl.row_count ?? 0).toLocaleString() }} 行
          </div>
        </div>
        <button
          class="text-[11px] text-blue-600 hover:text-blue-700 font-medium shrink-0"
          @click="$emit('view-data', tbl.table_name)"
        >
          查看数据 →
        </button>
        <button
          class="text-[11px] text-gray-500 hover:text-gray-700 font-medium shrink-0 border border-gray-200 rounded px-1.5 py-0.5"
          title="放大查看"
          @click="$emit('enlarge', tbl)"
        >
          <span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="11" cy="11" r="8" /> <path d="m21 21-4.3-4.3" /> </svg></span> 放大
        </button>
      </div>
      <!-- 字段明细（完整展示：名称 / 类型 / 主键 / 中文翻译） -->
      <div class="px-4 py-2 max-h-72 overflow-y-auto">
        <div
          v-for="col in tbl.columns"
          :key="col.name"
          class="flex items-start gap-2 py-1.5 border-b border-gray-50 last:border-0"
        >
          <span class="text-xs mt-0.5 shrink-0"><span v-if="col.primary_key"><span class="eico" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" > <circle cx="7.5" cy="15.5" r="5.5" /> <path d="m21 2-9.6 9.6" /> <path d="m15.5 7.5 3 3L22 7l-3-3" /> </svg></span></span><span v-else>·</span></span>
          <div class="min-w-0 flex-1">
            <div class="text-xs font-medium text-gray-700 font-mono">
              {{ col.name }}
              <span class="text-[10px] text-gray-400 font-normal ml-1">{{ col.type }}</span>
              <span v-if="!col.nullable" class="text-[9px] text-red-400 ml-1">NOT NULL</span>
            </div>
            <div class="text-[11px] text-gray-600 mt-0.5">{{ col.translation || col.comment || '—' }}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  tables: any[]
  loading?: boolean
}>()

defineEmits<{
  (e: 'view-data', table: string): void
  (e: 'enlarge', table: any): void
}>()
</script>
