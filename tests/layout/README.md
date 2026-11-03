# tests/layout — FPK 包布局坏例

`scripts/check_fpk_layout.py` 的回归测试。所有 fixture 由 `test_check_fpk_layout.py`
的 `build_good_package()` 在系统临时目录生成（stdlib，无提交的二进制），每个用例
复制一份"好包"后只改一处，再跑真实 CLI 断言退出码与规则编号。

## 运行

```bash
python -m unittest discover -s tests -v
# 或只跑布局用例
python -m unittest tests.layout.test_check_fpk_layout -v
```

## 覆盖的坏例（契约要求 ≥6，这里 8 个）

| 用例 | 构造方式 | 期望 |
|---|---|---|
| `test_missing_manifest` | 删除包根 `manifest` | exit 1 · `PKG-MANIFEST-01` |
| `test_platform_all_with_binary` | `platform="all"` + 写入 ELF x86_64 | exit 1 · `PKG-ARCH-01` |
| `test_invalid_json` | `config/privilege` 截断成非法 JSON | exit 1 · `PKG-PRIV-01` |
| `test_extra_root_file` | 包根新增 `README.md` | exit 1 · `PKG-ROOT-02` |
| `test_crlf_line_endings` | `cmd/main` 改为 CRLF | exit 1 · `PKG-ENC-02` |
| `test_pycache_junk` | 新增 `app/__pycache__/x.pyc` | exit 1 · `PKG-JUNK-01` |
| `test_other_dist_directory_still_junk` | 新增 `app/dist/bundle.js` | exit 1 · `PKG-JUNK-01` |
| `test_plugin_dashboard_dist_is_allowed` | 新增 `app/plugins/demo/dashboard/dist/*` | exit 0（手写插件源码，豁免） |

另有：好包通过、`--json` 结构、模板模式（缺 `app/webapp`/图标降级警告）、
源码树自动识别模板模式、目录不存在与缺参数的 exit 2。

## 规则速查

| 规则 | 级别 | 含义 |
|---|---|---|
| PKG-ROOT-01/02 | 违规 | 包根必需条目缺失 / 出现未允许条目 |
| PKG-JUNK-01 | 违规 | `__pycache__`、`*.pyc`、`.git`、`node_modules`、`dist`、`*.bak` 等垃圾 |
| PKG-MANIFEST-01..07 | 违规/警告 | manifest 存在性、必填字段、冻结值、禁用字段、`os_min_version` 警告、引号 |
| PKG-ARCH-01..03 | 违规/警告 | `platform=all` 禁止原生二进制；`x86` 需 x86_64；其它架构警告 |
| PKG-ENTRY-01..10 | 违规/警告 | 入口 JSON/字段/ID 前缀/图标/网关字段/直连端口/`desktop_applaunchname` |
| PKG-PRIV-01/02 | 违规 | `run-as=package`、固定 username/groupname、无 root/join-groups |
| PKG-RES-01/02 | 违规 | `config/resource` 合法且冻结为 `{}` |
| PKG-WIZARD-01..05 | 违规/警告 | 四份 JSON 数组、`type`/`field`、`wizard_*` 前缀、密码默认值警告 |
| PKG-ICON-01/03 | 违规/警告 | PNG 签名与 64/256 尺寸、可选 icon_512 尺寸警告 |
| PKG-CMD-01..05 | 违规/警告 | 九个生命周期脚本、删除旧代理、`pause_gateway.py`、多余文件警告 |
| PKG-APP-01..04 | 违规/警告 | `app/` 子目录白名单、必需 `ui`/`runtime`、`webapp`（模板模式警告） |
| PKG-RUNTIME-01..03 | 违规/警告 | `gateway_adapter.py`、`gateway.conf` 冻结值、`runtime/lib/libs.sh` 警告 |
| PKG-ENC-01/02 | 违规 | UTF-8 无 BOM、仅 LF |
| PKG-MODE-01 | 警告 | 未显式 `--template` 且缺 `app/webapp` 时按源码/模板模式检查 |
