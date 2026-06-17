---
name: "📦 版本发布"
about: 新建版本发布 checklist
title: "Release vX.Y.Z"
labels: release
assignees: ''

---

## Release vX.Y.Z

### 发布前检查

- [ ] 更新 `VERSION` 文件
- [ ] 更新 `README.md` 中的版本链接（如有）
- [ ] 确保所有待合并 PR 已合并
- [ ] 本地运行测试（`python web_serve.py`）
- [ ] 检查所有新功能的前后端联调

### 提交步骤

```bash
git add VERSION README.md <changed-files>
git commit -m "release: vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

### Release Notes 示例

```bash
gh release create vX.Y.Z \\
  --title "vX.Y.Z" \\
  --notes "### ✨ 新功能\n- ...\n\n### 🐛 修复\n- ...\n\n### 🔧 优化\n- ..."
```
