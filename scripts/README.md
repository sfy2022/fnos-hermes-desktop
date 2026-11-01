# 冒烟自检

不构建完整渲染层（约 1 分钟），验证打包链路是否正常。

```bash
bash scripts/smoke.sh
```

检查项：

1. `packaging/fpk/` 模板文件齐全
2. 所有 shell 脚本语法正确（`bash -n`）
3. 所有 JSON 文件合法（`app/ui/config`、`config/*`、`wizard/*`）
4. 用占位渲染层能成功打出 fpk
5. 产物包含 manifest / cmd / wizard 等必需条目

改动打包脚本后先跑这个，再跑完整构建。
