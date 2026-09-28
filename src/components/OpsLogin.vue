<template>
  <div class="login-page">
    <OpsWavyField />

    <header class="login-nav">
      <button class="login-brand" aria-label="返回首页" @click="emit('cancel')">
        <b aria-hidden="true"></b><span>Vequo 维阔</span><small>SIGN IN</small>
      </button>
      <button class="login-back" @click="emit('cancel')">← 返回首页</button>
    </header>

    <main class="login-main">
      <section class="login-intro">
        <p class="login-code"><span>00</span> BEFORE WORKSPACE</p>
        <h1>先登录，<br><em>再开始工作。</em></h1>
        <p class="login-summary">用注册邮箱与密码登录，按账号角色访问数据。忘记密码请联系管理员重置；没有账号可以先注册一个。</p>

        <div class="login-note"><i></i><span>ACCOUNT REQUIRED</span><b>登录后进入工作台</b></div>
      </section>

      <section class="login-panel">
        <div class="panel-heading"><span>SIGN / IN</span><small>使用注册邮箱登录</small></div>
        <form @submit.prevent="submitLogin">
          <label
            v-for="(field, index) in fields"
            :key="field.key"
            class="form-row"
            :class="{ 'is-active': activeIndex === index, 'is-missing': attemptedSubmit && !form[field.key].trim() }"
          >
            <span class="row-copy"><small>{{ field.en }}</small><b>{{ field.label }}</b></span>
            <input
              v-model="form[field.key]"
              :type="field.type"
              :placeholder="field.placeholder"
              :autocomplete="field.key === 'password' ? 'current-password' : 'username'"
              @focus="activeIndex = index"
              @input="activeIndex = index; attemptedSubmit = false; loginError = ''"
            >
          </label>

          <p v-if="loginError" class="login-error" role="alert">{{ loginError }}</p>

          <div class="form-actions">
            <p :class="{ 'has-error': attemptedSubmit && !allFilled }"><span>*</span> {{ attemptedSubmit && !allFilled ? '请填写邮箱与密码后再登录' : '邮箱与密码均为必填。' }}</p>
            <button type="submit" :disabled="loggingIn"><span>{{ loggingIn ? '正在登录' : '登 录 进 入 工 作 台' }}</span><b>↗</b></button>
          </div>

          <div class="login-alt">
            没有账号？<button type="button" @click="emit('register')">立即注册</button>
          </div>
        </form>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import OpsWavyField from './OpsWavyField.vue'
import { login as apiLogin, type AuthUser } from '../auth'

const props = defineProps<{ initialEmail?: string }>()
// 登录成功后由 App.vue 统一处理（写 authUser / 记录登录 / 切到工作台），
// 失败的错误文案留在本组件内展示，避免把表单态提升到 App。
const emit = defineEmits<{ success: [user: AuthUser]; register: []; cancel: [] }>()

type FieldKey = 'email' | 'password'
const fields: { key: FieldKey; label: string; en: string; placeholder: string; type: string }[] = [
  { key: 'email', label: '邮箱', en: 'EMAIL', placeholder: '注册时使用的邮箱', type: 'text' },
  { key: 'password', label: '密码', en: 'PASSWORD', placeholder: '登录密码', type: 'password' },
]
// 从注册页带回来的邮箱直接预填，用户只需输密码
const form = reactive<Record<FieldKey, string>>({ email: props.initialEmail || '', password: '' })
const activeIndex = ref(0)
const attemptedSubmit = ref(false)
const loginError = ref('')
const loggingIn = ref(false)

const allFilled = computed(() => form.email.trim().length > 0 && form.password.length > 0)

async function submitLogin() {
  if (loggingIn.value) return
  const email = form.email.trim()
  if (!email || !form.password) {
    attemptedSubmit.value = true
    loginError.value = '请输入邮箱和密码'
    return
  }
  loggingIn.value = true
  loginError.value = ''
  try {
    // 登录严格走后端：后端没有该账户就报错，绝不降级到 guest
    // （guest 无权限策略会被当作「全库可见」，破坏「注册账户默认无授权」设计）。
    emit('success', await apiLogin(email, form.password))
  } catch (e: any) {
    loginError.value = e?.message || '邮箱或密码错误，如未注册请先注册'
    form.password = ''
    loggingIn.value = false
  }
  // 成功时不复位 loggingIn：组件随即被 App 切走，保持按钮禁用避免重复提交
}
</script>

<style scoped>
.login-page { position: relative; isolation: isolate; min-height: 100svh; overflow: hidden; color: #1d2129; background: #f7f8fa; font-family: Arial, 'Noto Sans CJK SC', sans-serif; }
.login-page:before { position: absolute; z-index: 0; inset: 0; content: ''; pointer-events: none; background: radial-gradient(circle at 12% 82%, rgba(255, 119, 94, .16), transparent 30%), radial-gradient(circle at 78% 15%, rgba(77, 158, 255, .42), transparent 31%); }
button { color: inherit; }

.login-nav { position: relative; z-index: 5; display: flex; align-items: center; justify-content: space-between; padding: 24px clamp(22px, 4.5vw, 72px); border-bottom: 1px solid rgba(29, 33, 41, .16); }
.login-brand { display: flex; min-height: 44px; align-items: center; gap: 10px; border: 0; background: transparent; cursor: pointer; }
.login-brand b { display: block; width: 46px; height: 46px; background: url('/brand/vequo-app-icon.svg') center/contain no-repeat; }
.login-brand span { font: 700 20px Rajdhani, sans-serif; letter-spacing: .18em; }
.login-brand small { padding-left: 10px; border-left: 1px solid rgba(29, 33, 41, .22); font: 600 8px Rajdhani, sans-serif; letter-spacing: .15em; opacity: .58; }
.login-back { min-height: 44px; padding: 0 18px; border: 1px solid #1d2129; border-radius: 24px; background: rgba(243, 241, 232, .55); font-size: 12px; backdrop-filter: blur(10px); cursor: pointer; }
.login-back:hover { color: #f7f8fa; background: #1d2129; }

.login-main { position: relative; z-index: 2; display: grid; grid-template-columns: minmax(360px, .82fr) minmax(520px, 1.18fr); gap: 7vw; min-height: calc(100svh - 87px); align-items: center; padding: 55px clamp(28px, 6vw, 96px) 64px; }
.login-code { display: flex; align-items: center; gap: 12px; margin: 0 0 26px; font: 600 9px Rajdhani, sans-serif; letter-spacing: .18em; }
.login-code span { display: grid; width: 27px; height: 27px; place-items: center; border: 1px solid currentColor; border-radius: 50%; letter-spacing: 0; }
.login-intro h1 { margin: 0; font-size: clamp(56px, 6.2vw, 104px); font-weight: 400; line-height: .91; letter-spacing: -.065em; }
.login-intro h1 em { color: #1677ff; font-family: Georgia, 'Songti SC', serif; font-weight: 400; }
.login-summary { max-width: 570px; margin: 31px 0 0; color: #4e5969; font-size: 14px; line-height: 1.9; }
.login-note { display: grid; grid-template-columns: auto 1fr; gap: 3px 9px; align-items: center; width: max-content; margin-top: 40px; padding: 13px 16px; border: 1px solid rgba(29, 33, 41, .2); border-radius: 15px; background: rgba(255, 255, 255, .35); backdrop-filter: blur(12px); }
.login-note i { grid-row: 1/3; width: 8px; height: 8px; border-radius: 50%; background: #4D9EFF; box-shadow: 0 0 0 5px rgba(77, 158, 255, .14); }
.login-note span { font: 600 7px Rajdhani, sans-serif; letter-spacing: .16em; }
.login-note b { font-size: 11px; font-weight: 500; }

.login-panel { position: relative; padding: 34px 36px 30px; border: 1px solid rgba(29, 33, 41, .23); border-radius: 32px; background: rgba(247, 248, 250, .66); box-shadow: 0 35px 90px rgba(29, 33, 41, .1); backdrop-filter: blur(18px); }
.panel-heading { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 8px; padding-bottom: 17px; border-bottom: 1px solid rgba(29, 33, 41, .16); }
.panel-heading span { font: 600 11px Rajdhani, sans-serif; letter-spacing: .16em; }
.panel-heading small { color: #86909c; font-size: 11px; }

.form-row { display: grid; grid-template-columns: minmax(0, 1fr); gap: 9px; padding: 20px 12px 19px; border-bottom: 1px solid rgba(29, 33, 41, .12); border-radius: 16px; transition: background .2s, border-color .2s; }
.form-row.is-active { border-color: transparent; background: rgba(77, 158, 255, .07); }
.form-row.is-missing { background: rgba(245, 34, 45, .06); }
.row-copy { display: flex; align-items: baseline; gap: 9px; }
.row-copy small { color: #86909c; font: 600 8px Rajdhani, sans-serif; letter-spacing: .14em; }
.row-copy b { font-size: 14px; font-weight: 600; }
.form-row input { width: 100%; height: 46px; padding: 0 15px; border: 1px solid rgba(29, 33, 41, .2); border-radius: 14px; background: rgba(255, 255, 255, .72); color: inherit; font-size: 14px; outline: none; transition: border-color .18s, box-shadow .18s; }
.form-row input:focus { border-color: #4D9EFF; box-shadow: 0 0 0 4px rgba(77, 158, 255, .14); }

.login-error { margin: 16px 0 0; padding: 10px 13px; border: 1px solid rgba(245, 34, 45, .24); border-radius: 12px; background: rgba(245, 34, 45, .06); color: #d03050; font-size: 12px; }
.form-actions { display: grid; gap: 13px; margin-top: 24px; }
.form-actions p { margin: 0; color: #86909c; font-size: 11px; }
.form-actions p span { margin-right: 5px; color: #f5222d; }
.form-actions p.has-error { color: #d03050; }
.form-actions button { display: flex; min-height: 54px; align-items: center; justify-content: center; gap: 10px; border: 0; border-radius: 16px; background: #4D9EFF; color: #fff; font-size: 14px; font-weight: 600; cursor: pointer; transition: background .18s, transform .18s; }
.form-actions button:hover:not(:disabled) { background: #2E7CF0; transform: translateY(-1px); }
.form-actions button:disabled { opacity: .62; cursor: not-allowed; }
.login-alt { display: flex; justify-content: center; gap: 6px; margin-top: 18px; padding-top: 16px; border-top: 1px solid rgba(29, 33, 41, .12); color: #86909c; font-size: 12px; }
.login-alt button { border: 0; background: transparent; color: #1677ff; font-size: 12px; font-weight: 600; cursor: pointer; }
.login-alt button:hover { text-decoration: underline; }

@media (max-width: 960px) {
  .login-main { display: block; padding: 34px 20px 48px; }
  .login-intro h1 { font-size: clamp(40px, 11vw, 64px); }
  .login-summary { margin-top: 20px; font-size: 13px; }
  .login-note { margin-top: 22px; }
  .login-panel { margin-top: 30px; padding: 22px 18px 20px; border-radius: 24px; }
}
</style>
