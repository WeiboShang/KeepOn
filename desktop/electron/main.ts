import { app, BrowserWindow, Notification, ipcMain, shell } from 'electron'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

app.setName('KeepOn')
let approvalWindow: BrowserWindow | null = null
let mainWindow: BrowserWindow | null = null
const acceptanceToken = process.env.KEEPON_ACCEPTANCE_TOKEN
delete process.env.KEEPON_ACCEPTANCE_TOKEN
const acceptanceMethods = new Set([
  'conversation.send', 'run.recover', 'task.spec_revise', 'task.plan_accept', 'task.plan_reject',
  'acceptance.review', 'acceptance.retry',
])
const indexFile = path.join(__dirname, '../../dist/index.html')

function surfaceUrl(hash = '') {
  const developmentUrl = process.env.VITE_DEV_SERVER_URL || (!app.isPackaged ? 'http://127.0.0.1:5173' : '')
  return developmentUrl ? `${developmentUrl}${hash}` : ''
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1440, height: 900, minWidth: 980, minHeight: 650, title: 'KeepOn',
    backgroundColor: '#eef3f8',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false },
  })
  mainWindow = win
  win.on('closed', () => { if (mainWindow === win) mainWindow = null })
  const developmentUrl = surfaceUrl()
  if (developmentUrl) win.loadURL(developmentUrl)
  else win.loadFile(indexFile)
}

// 高风险桌面操作使用独立置顶窗口，避免审批提示被主窗口遮住。
function showApprovalWindow() {
  if (!approvalWindow) {
    approvalWindow = new BrowserWindow({
      width: 430, height: 390, minWidth: 380, minHeight: 330,
      title: 'KeepOn · 操作确认', alwaysOnTop: true, resizable: true,
      backgroundColor: '#f4f7fb', show: false,
      webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false },
    })
    const url = surfaceUrl('#approval')
    if (url) void approvalWindow.loadURL(url)
    else void approvalWindow.loadFile(path.join(__dirname, '../../dist/index.html'), { hash: 'approval' })
    approvalWindow.on('closed',()=>{approvalWindow=null})
  }
  approvalWindow.show()
  approvalWindow.focus()
}

app.whenReady().then(() => {
  ipcMain.handle('acceptance-rpc', async (event, method: string, params: Record<string, unknown>) => {
    const frame = event.senderFrame
    const expected = new URL(surfaceUrl() || pathToFileURL(indexFile).href)
    const current = frame ? new URL(frame.url) : null
    if (!mainWindow || event.sender !== mainWindow.webContents || !frame || frame.parent
      || current?.origin !== expected.origin || current.protocol !== expected.protocol || current.pathname !== expected.pathname
      || !acceptanceMethods.has(method)) {
      throw new Error('验收操作只允许从应用主窗口发起。')
    }
    if (!acceptanceToken || acceptanceToken.length < 32) {
      throw new Error('验收写操作需要桌面启动凭据。请使用项目启动脚本。')
    }
    const port = Number(process.env.KEEPON_PORT || 8765)
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Host 端口配置无效。')
    const timeout = method === 'conversation.send' || method === 'run.recover' ? 30 * 60_000 : 30_000
    const response = await fetch(`http://127.0.0.1:${port}/acceptance/rpc`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(timeout),
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${acceptanceToken}` },
      body: JSON.stringify({ method, params }),
    })
    if (!response.ok) throw new Error(response.status === 403 ? '验收授权失败。请确认桌面端和 Host 从同一启动会话启动。' : 'Host 无法处理验收请求。')
    return response.json()
  })
  ipcMain.handle('open-external', (_e, url: string) => shell.openExternal(url))
  ipcMain.handle('notify', (_e, title: string, body: string) => new Notification({ title, body }).show())
  ipcMain.handle('show-approval', () => showApprovalWindow())
  ipcMain.handle('hide-approval', () => approvalWindow?.hide())
  createWindow(); app.on('activate', () => { if (!BrowserWindow.getAllWindows().length) createWindow() })
})
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit() })
