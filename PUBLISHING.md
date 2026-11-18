# 发布到 GitHub

## 当前状态（本地已就绪）

- `main` = **单个干净提交**（`chore: initial public release v0.14.4`），共 200 个文件
- 旧历史（含 SSID/IP/UDID/个人模板的 48 个提交）已备份到 **`dist/private-history.bundle`**
  （3.5 MB，已被 `.gitignore` 排除，不会发布），并从仓库 refs 中移除
- 旧里程碑 tag 已删除，只保留 `v0.14.4`
- `origin` 已指向 `git@github.com:ldchaowin/assistantAgent.git`（SSH 形式，可改）

## 发布步骤

```bash
cd /path/to/assistantAgent

# 1) 预览将要发布的文件
git ls-tree -r --name-only main | wc -l     # 应为 200
git status --short                          # 应为空

# 2) 在 GitHub 建一个空仓库 `assistantAgent`（不要勾选 README/gitignore/license），然后：
git push -u origin main
git push origin v0.14.4

# 3) GitHub → Settings → Branches：确认默认分支是 main
#    然后看 Actions 是否启动 CI（Python + 插件测试）
```

如果仓库名/用户名不是 `ldchaowin/assistantAgent`，改三处 URL：根 `README.md`、
`dsh-plugin-phone/package.json`（repository/homepage/bugs）、`console/pyproject.toml`（urls），
再 `git commit -am "chore: point urls at public repo"` 后推送。

> 备份文件 `dist/private-history.bundle` 只用于本地找回旧历史；**不要**把它提交或上传。
> 需要恢复时：`git clone dist/private-history.bundle private-history-restore`。

如果坚持保留历史，则必须先用 `git filter-repo` 清掉上述字符串并强推——不推荐，
因为旧历史里的个人模板和截图信息很难彻底确认。

## 发布前人工核对

- [ ] `git ls-files | grep -E "runs/|shots/|\.venv|\.tools|\.pio"` 为空
- [ ] 用 `git grep` 检查本机路径、SSID、内网 IP、设备序列号均无结果
- [ ] `templates/` 未被跟踪（个人模板只保留在本地，示例在 `examples/templates/`）
- [ ] `dsh-plugin-phone/cordis.patch.yml` 中的 `/path/to/assistantAgent` 仍是占位符（不要把本机路径改回去提交）
- [ ] `LICENSE`、`NOTICE`、`SECURITY.md` 存在
- [ ] README 里的 clone URL、徽章、仓库名与实际一致
- [ ] GitHub 仓库设置：About 描述、topics、关闭 wiki（如需）、启用 security advisories
- [ ] 打 tag：`git tag v0.14.4 && git push origin v0.14.4`

## 之后发布 npm 包（可选）

插件本身可以作为 npm 包分发（`package.json` 已配置 `files`、`dsh.bundle`、`dsh.client`）：

```bash
cd dsh-plugin-phone
npm pack --dry-run          # 检查内容：lib/ client/ cordis.patch.yml README LICENSE
npm publish                 # 需要 npm 账号；scope 按需修改包名
```

## 发布后

- 在 README 里补 GitHub Release 链接（可选）
- 后续改动请只提交源码；`runs/`、`shots/`、`templates/`、`.venv/`、`.pio/` 已在 `.gitignore`
