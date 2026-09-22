## Git 分支与 PR 工作流（必须遵守）

### 基本规则
- `main` 是受保护的稳定分支，**禁止直接在 main 上修改、提交或推送**。
- 每个功能、修复都在独立分支上开发，完成后通过 PR 合并到 main。
- PR 的审核与合并由团队成员在 GitHub 上完成，你的职责到创建 PR 为止。
- **未经我明确确认，不得执行 `git push`、创建 PR、删除分支、`git reset --hard`、`git push --force` 等操作。**
- **任何情况下都不要审核、评论、approve 或合并 PR**，不要执行 `gh pr review`、`gh pr merge`。

### 开始任务时
1. 先运行 `git status` 和 `git branch --show-current` 检查当前状态。
2. 如果工作区有未提交的改动，先告诉我，不要自行丢弃或覆盖。
3. 如果当前在 main 上，先同步再新建分支：
   git switch main
   git pull
   git switch -c <分支名>
4. 如果本地还留有已合并的旧分支，可以顺手清理：
   git branch -d <已合并的分支名>
5. 分支命名：
   - 新功能：`feature/简短描述`（如 `feature/tin-trend-chart`）
   - 修复：`fix/简短描述`
   - 文档：`docs/简短描述`
   - 重构：`refactor/简短描述`
   使用小写英文和连字符。
6. 如果不确定应该新建分支还是继续用当前分支，先问我。

### 开发过程中
- 按逻辑小步提交，每个 commit 只做一件事。
- commit 信息格式：`类型: 中文描述`，类型用 feat / fix / docs / refactor / style / chore。
  例：`feat: 新增锡价趋势图组件`
- 只 `git add` 与本次任务相关的文件，不要用 `git add .` 把无关文件一起提交。
- 不要提交密钥、`.env`、本地配置、临时文件。

### 任务结束时（必须停下来和我确认）
开发完成后，**不要自动推送或创建 PR**，先向我汇报以下内容，然后等待我的确认：
1. 当前分支名
2. 本次所有提交（`git log main..HEAD --oneline`）
3. 改动文件汇总（`git diff main --stat`）
4. 做了什么、为什么这样做、有哪些没做完或需要我注意的地方
5. 是否做过测试或本地运行验证，结果如何
6. 拟定的 PR 标题和描述草稿

只有在我明确回复"确认"/"可以发 PR"之后，才执行：
   git fetch origin
   git merge origin/main        # 有冲突先告诉我，不要自行大范围解决
   git push -u origin <分支名>
   gh pr create --base main --title "<标题>" --body "<描述>"

PR 创建成功后，本次任务即结束，不需要再做其他操作。

### PR 描述模板
## 改动内容
- ...
## 改动原因
- ...
## 测试情况
- ...
## 注意事项
- ...

### 根据审核意见修改时
- 如果我转达了审核意见，在**同一个分支**上修改并提交，不要新建分支或新建 PR。
- 修改完成后同样先向我汇报改动，确认后再 `git push`，PR 会自动更新。

### 版本发布
- 只有在我明确要求时才打标签：
   git tag -a vX.Y.Z -m "版本说明"
   git push origin vX.Y.Z