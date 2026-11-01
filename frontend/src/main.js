// 入口模块：先把 Vue 视图工厂挂到 window，再启动原 app.js。
// vm 子进程测试只加载 app.js 本身，不经过本入口。
import { createTemplatesView } from './templates/index'

window.__createTemplatesView = createTemplatesView

import './app.js'
