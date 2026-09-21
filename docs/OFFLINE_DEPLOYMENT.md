# GitHub Artifact 离线交付

当公司 Linux 无法访问 `github.com` 时，不运行 self-hosted runner。GitHub Actions 在托管
runner 上完成测试并制作离线交付包，再由人工将包复制进内网。

## 交付包内容

每次向 `main` 或 `cicd` 推送后，Actions 中的 `Build offline Linux bundle` 任务生成：

```text
commodity-research-linux-x64-py312-<commit>.tar.gz
commodity-research-linux-x64-py312-<commit>.tar.gz.sha256
```

压缩包包含：

- 完整应用源码、Alembic 迁移、种子和部署配置
- Linux x86_64、Python 3.12 对应的全部 wheels
- 离线安装与版本切换脚本
- Git commit 标识和目标平台说明

Python 解释器、systemd、rsync 和 curl 属于操作系统基础组件，不包含在交付包中。

## 1. 下载与传输

在可访问 GitHub 的电脑打开：

```text
GitHub repository -> Actions -> 对应 workflow run -> Artifacts
```

下载 `commodity-research-linux-x64-<SHA>`，解压 GitHub 外层 zip，得到 `.tar.gz` 和
`.sha256`。通过公司批准的文件传输方式复制到 Linux，例如：

```bash
scp commodity-research-linux-x64-py312-*.tar.gz* root@INTERNAL_SERVER:/opt/delivery/
```

## 2. 校验与解压

在 Linux 上：

```bash
cd /opt/delivery
sha256sum -c commodity-research-linux-x64-py312-*.tar.gz.sha256
tar -xzf commodity-research-linux-x64-py312-*.tar.gz
cd commodity-research-linux-x64-py312-*
```

校验必须显示 `OK`。如果失败，不要安装。

## 3. 首次初始化

服务器需要预装 Python 3.12、`python3.12-venv`、curl、rsync、sudo 和 systemd。
当前以 root 操作时，可以直接让 root 承担人工发布：

```bash
RUNNER_USER=root bash app/deploy/bootstrap.sh
vi /etc/commodity-research-platform/app.env
```

默认配置使用持久化 SQLite：

```text
TIN_DATABASE_URL=sqlite:////var/lib/commodity-research-platform/data/tin.db
```

多人并发使用时建议改为 PostgreSQL。数据库和导出文件位于 `/var/lib`，后续安装新版本
不会覆盖。

## 4. 离线安装

每个版本执行：

```bash
bash app/deploy/install-offline.sh
```

安装脚本会：

1. 从包内 wheelhouse 创建 venv，全程不访问 PyPI。
2. 执行数据库迁移和幂等种子初始化。
3. 原子切换 `/opt/commodity-research-platform/current`。
4. 重启 systemd 服务并请求 `/healthz`。
5. 健康检查失败时回切上一版本。
6. 保留最近五个版本。

验证：

```bash
systemctl status commodity-research-platform
curl --fail http://127.0.0.1:8765/healthz
readlink -f /opt/commodity-research-platform/current
```

## 5. 自动部署开关

Workflow 默认只构建离线包，不等待内网 runner。以后网络放通后，在 GitHub 仓库变量中设置：

```text
ENABLE_SELF_HOSTED_DEPLOY=true
```

即可同时恢复 self-hosted runner 自动部署。删除该变量或设为 `false` 后继续使用离线交付。
