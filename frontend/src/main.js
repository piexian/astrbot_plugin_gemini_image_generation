// 入口模块：先把 Vue 视图工厂挂到 window，再启动原 app.js。
// vm 子进程测试只加载 app.js 本身，不经过本入口。
import { createLimitsView } from './limits/index'
import { createProviderConfigView } from './provider-config/index'
import { createTemplatesView } from './templates/index'

window.__createTemplatesView = createTemplatesView
window.__createProviderConfigView = createProviderConfigView
window.__createLimitsView = createLimitsView

import './app.js'
