"""应用更新 — 检查 GitHub Releases 并升级本应用。

* 界面：``desktop/plugin.js``（构建时注入 bundled），提供「应用更新」页
  与右下角状态栏提示。
* 后端：``dashboard/plugin_api.py``，挂载 ``/api/plugins/app-updater/``。

本插件不注册 agent 工具：升级会重启应用、中断会话，交给用户点按钮更安全。
"""
