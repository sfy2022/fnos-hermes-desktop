/**
 * Hermes Dashboard — fnOS 授权面板（bundled plugin）
 *
 * 纯 IIFE，无构建步骤。通过 window.__HERMES_PLUGIN_SDK__ 拿 React 与 authed fetch，
 * 后端在 dashboard/plugin_api.py（/api/plugins/fnos-auth/），
 * 真正的授权逻辑在本地守护进程 fnos-authd（127.0.0.1:9137）。
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  var React = SDK.React;
  var h = React.createElement;
  var API = "/api/plugins/fnos-auth";

  function call(path, opts) {
    return SDK.fetchJSON(API + path, opts);
  }

  function pillClass(d) {
    if (d.authorized) return "fa-pill fa-pill--ok";
    if (d.reason === "连接失败") return "fa-pill fa-pill--wait";
    return "fa-pill fa-pill--bad";
  }

  function pillText(d) {
    if (d.authorized) return "已授权";
    return d.reason || "未授权";
  }

  function fmtLeft(deadline, now) {
    var left = Math.max(0, Math.floor(deadline - now / 1000));
    var m = Math.floor(left / 60);
    var s = left % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function DeviceCard(props) {
    var d = props.device;
    var flow = props.flow;
    var msg = props.msg;
    var busy = props.busy;
    var now = props.now;

    var actions = [
      h("button", {
        key: "auth",
        className: "fa-btn fa-btn--primary",
        disabled: busy === d.id,
        onClick: function () { props.onStart(d.id); }
      }, d.authorized ? "重新授权" : "授权"),
      h("button", {
        key: "check",
        className: "fa-btn",
        disabled: busy === d.id,
        onClick: function () { props.onCheck(d.id); }
      }, busy === d.id ? "检查中…" : "检查")
    ];
    if (d.authorized) {
      actions.push(h("button", {
        key: "logout", className: "fa-btn fa-btn--danger",
        onClick: function () { props.onLogout(d.id); }
      }, "撤销授权"));
    }
    if (!d.builtin) {
      actions.push(h("button", {
        key: "del", className: "fa-btn fa-btn--danger",
        onClick: function () { props.onDelete(d.id); }
      }, "删除设备"));
    }

    var meta = d.authorized
      ? h("div", { className: "fa-meta" },
          d.user || "",
          d.admin ? " · 管理员" : "",
          d.host_name ? " · " + d.host_name + " v" + (d.version || "") : "")
      : null;

    var flowPanel = null;
    if (flow) {
      var expired = flow.deadline * 1000 <= now;
      flowPanel = h("div", { className: "fa-flow" },
        h("div", { className: "fa-flow-top" },
          flow.qr
            ? h("img", { className: "fa-qr", src: flow.qr, alt: "扫码授权", width: 132, height: 132 })
            : null,
          h("div", { className: "fa-flow-info" },
            h("a", {
              className: "fa-link",
              href: flow.url,
              target: "_blank",
              rel: "noopener noreferrer"
            }, "打开 fnOS 授权页 ↗"),
            h("div", { className: "fa-hint" }, "手机扫码登录，回来后粘贴授权码"),
            h("div", { className: expired ? "fa-timer fa-timer--bad" : "fa-timer" },
              expired ? "授权码已过期" : "剩余 " + fmtLeft(flow.deadline, now)))),
        h("div", { className: "fa-code" },
          h("input", {
            type: "password",
            className: "fa-input",
            placeholder: "粘贴授权码（不回显）",
            autoComplete: "new-password",
            value: props.code,
            disabled: expired,
            onChange: function (e) { props.onCode(d.id, e.target.value); }
          }),
          h("button", {
            className: "fa-btn fa-btn--primary",
            disabled: expired || busy === d.id,
            onClick: function () { props.onSubmit(d.id); }
          }, "提交")),
        msg ? h("div", { className: "fa-msg fa-msg--" + (msg.kind || "info") }, msg.text) : null);
    }

    return h("div", { className: "fa-card" },
      h("div", { className: "fa-card-head" },
        h("div", { className: "fa-title-wrap" },
          h("div", { className: "fa-title" }, d.name),
          h("div", { className: "fa-addr" }, d.scheme + "://" + d.host + ":" + d.port)),
        h("div", { className: pillClass(d) }, pillText(d))),
      meta,
      h("div", { className: "fa-actions" }, actions),
      flowPanel);
  }

  function FnosAuthPage() {
    var [data, setData] = React.useState(null);
    var [err, setErr] = React.useState("");
    var [busy, setBusy] = React.useState("");
    var [flows, setFlows] = React.useState({});
    var [codes, setCodes] = React.useState({});
    var [msgs, setMsgs] = React.useState({});
    var [target, setTarget] = React.useState("");
    var [name, setName] = React.useState("");
    var [addMsg, setAddMsg] = React.useState(null);
    var [now, setNow] = React.useState(Date.now());

    var refresh = React.useCallback(function (hard) {
      call("/devices" + (hard ? "?refresh=1" : ""))
        .then(function (j) { setData(j); setErr(""); })
        .catch(function (e) { setErr(e && e.message ? e.message : String(e)); });
    }, []);

    React.useEffect(function () { refresh(false); }, [refresh]);
    React.useEffect(function () {
      var t = setInterval(function () { setNow(Date.now()); }, 1000);
      return function () { clearInterval(t); };
    }, []);

    function setMsg(id, kind, text) {
      setMsgs(function (m) {
        var next = Object.assign({}, m);
        next[id] = { kind: kind, text: text };
        return next;
      });
    }
    function clearMsg(id) {
      setMsgs(function (m) {
        var next = Object.assign({}, m);
        delete next[id];
        return next;
      });
    }

    function onStart(id) {
      setBusy(id);
      clearMsg(id);
      call("/devices/" + encodeURIComponent(id) + "/start", { method: "POST" })
        .then(function (r) {
          setFlows(function (f) {
            var next = Object.assign({}, f);
            next[id] = {
              url: r.url,
              qr: r.qr || null,
              deadline: Date.now() / 1000 + (r.ttl || 300)
            };
            return next;
          });
        })
        .catch(function (e) { setMsg(id, "error", e && e.message ? e.message : String(e)); })
        .then(function () { setBusy(""); });
    }

    function onSubmit(id) {
      var code = (codes[id] || "").trim();
      if (!code) { setMsg(id, "error", "请先粘贴授权码"); return; }
      setBusy(id);
      setMsg(id, "info", "正在交换令牌…");
      call("/devices/" + encodeURIComponent(id) + "/code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code })
      })
        .then(function (r) {
          setFlows(function (f) {
            var next = Object.assign({}, f);
            delete next[id];
            return next;
          });
          setCodes(function (c) {
            var next = Object.assign({}, c);
            delete next[id];
            return next;
          });
          clearMsg(id);
          refresh(true);
          setAddMsg({ kind: "ok", text: "授权成功：" + (r.user || "") + (r.admin ? "（管理员）" : "") });
          setTimeout(function () { setAddMsg(null); }, 4000);
        })
        .catch(function (e) { setMsg(id, "error", e && e.message ? e.message : String(e)); })
        .then(function () { setBusy(""); });
    }

    function onCheck(id) {
      setBusy(id);
      call("/devices/" + encodeURIComponent(id))
        .then(function () { refresh(false); })
        .catch(function (e) { setMsg(id, "error", "检查失败：" + (e && e.message ? e.message : e)); })
        .then(function () { setBusy(""); });
    }

    function onLogout(id) {
      if (!window.confirm("撤销这台设备在本机保存的授权？")) return;
      setBusy(id);
      call("/devices/" + encodeURIComponent(id) + "/logout", { method: "POST" })
        .then(function () { refresh(true); })
        .catch(function (e) { setMsg(id, "error", e && e.message ? e.message : e); })
        .then(function () { setBusy(""); });
    }

    function onDelete(id) {
      if (!window.confirm("删除这台设备的记录？不影响设备本身。")) return;
      call("/devices/" + encodeURIComponent(id) + "/delete", { method: "POST" })
        .then(function () { refresh(true); })
        .catch(function (e) { setMsg(id, "error", e && e.message ? e.message : e); });
    }

    function onAdd() {
      if (!target.trim()) { setAddMsg({ kind: "error", text: "请填写 IP 或域名" }); return; }
      setAddMsg({ kind: "info", text: "添加中…" });
      call("/devices", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: target.trim(), name: name.trim(), scheme: "auto" })
      })
        .then(function (r) {
          setTarget("");
          setName("");
          setAddMsg({ kind: "ok", text: "已添加 " + r.device.host + ":" + r.device.port });
          refresh(true);
          setTimeout(function () { setAddMsg(null); }, 4000);
        })
        .catch(function (e) { setAddMsg({ kind: "error", text: e && e.message ? e.message : e }); });
    }

    var devices = (data && data.devices) || [];

    return h("div", { className: "fa-page" },
      h("div", { className: "fa-head" },
        h("div", null,
          h("h2", { className: "fa-h2" }, "fnOS 授权"),
          h("div", { className: "fa-sub" },
            "多设备 OAuth 授权" + (data && data.lan ? " · 本机 " + data.lan : ""))),
        h("button", { className: "fa-btn", onClick: function () { refresh(true); } }, "刷新")),

      err ? h("div", { className: "fa-msg fa-msg--error" }, err) : null,

      h("div", { className: "fa-add" },
        h("input", {
          className: "fa-input fa-input--grow",
          placeholder: "IP 或域名，例如 192.168.1.28 / nas.example.com",
          value: target,
          onChange: function (e) { setTarget(e.target.value); },
          onKeyDown: function (e) { if (e.key === "Enter") onAdd(); }
        }),
        h("input", {
          className: "fa-input",
          placeholder: "备注（可选）",
          value: name,
          onChange: function (e) { setName(e.target.value); }
        }),
        h("button", { className: "fa-btn fa-btn--primary", onClick: onAdd }, "添加")),
      addMsg ? h("div", { className: "fa-msg fa-msg--" + addMsg.kind }, addMsg.text) : null,

      devices.length === 0
        ? h("div", { className: "fa-empty" }, "还没有设备，先在上面添加一台")
        : devices.map(function (d) {
            return h(DeviceCard, {
              key: d.id,
              device: d,
              flow: flows[d.id] || null,
              code: codes[d.id] || "",
              msg: msgs[d.id] || null,
              busy: busy,
              now: now,
              onStart: onStart,
              onSubmit: onSubmit,
              onCheck: onCheck,
              onLogout: onLogout,
              onDelete: onDelete,
              onCode: function (id, v) {
                setCodes(function (c) {
                  var next = Object.assign({}, c);
                  next[id] = v;
                  return next;
                });
              }
            });
          }),

      h("div", { className: "fa-foot" },
        "授权码 5 分钟内有效、仅可使用一次 · 账号密码只在 fnOS 官方登录页输入 · ",
        "令牌由 trim-cli 保存在本机 profile 中"));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("fnos-auth", FnosAuthPage);
  }
})();
