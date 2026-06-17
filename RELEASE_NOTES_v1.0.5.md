## v1.0.5

### 🐛 Bug 修复
- PyInstaller 冻结模式找不到 VERSION 文件，启动崩溃
- exe 打包版打赏二维码图片不显示（alipay.jpg / wechat.png 未打包进 exe）
- B站 API `x/space/wbi/acc/info` 接口签名失效，改用 `x/web-interface/card`
- 版本号显示双 v 前缀（显示为 vv1.0.1）
- v-else 未紧邻 v-if 导致 Vue 编译失败、前端白屏
- 配置页 div 结构破损导致「功能开关」之后的所有区域无白色背景

### ✨ 新功能
- **检查更新** — 帮助页一键检测 GitHub 新版本，弹窗引导前往 Releases 页面下载
- **新建房间自动查用户名** — 输入主播 UID 时自动从 B站 获取昵称

### 🔧 优化
- 弹幕总条数上限默认 1000 → 10000
- 所有模态框防止外部点击关闭
- 检查更新弹窗精简（不显示更新日志）
- 帮助页 UI 精简，移除冗余功能特性列表
- Release Notes 改为手工撰写，信息更清晰

### 🛠 工程化
- VERSION 文件自动打包进 exe（`--add-data`）
- `_figs_path()` 统一处理冻结模式下的图片路径
- 新增发布 Issue 模板和 Release 自动生成配置（`.github/`）
- README 更新日志迁移至 GitHub Releases
