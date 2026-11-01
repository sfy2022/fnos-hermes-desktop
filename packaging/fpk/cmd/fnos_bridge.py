"""fnOS 网关前缀桥接脚本（注入到页面）

问题
----
应用挂在 /app/hermes-desktop 子路径下，但前端代码里用的是**站点根绝对路径**：
    fetch('/auth/password-login')
    fetch('/api/status')
    new WebSocket('/api/ws')
在 iframe 里，页面地址是 http://<host>:5666/app/hermes-desktop/，
浏览器会把这些绝对路径解析成 http://<host>:5666/auth/password-login ——
那是 fnOS 网关自己的路径，请求根本到不了应用，于是登录报
"Sign-in failed"、界面各项接口全部 404。

解决
----
在页面里注入本脚本，hook fetch / XMLHttpRequest / WebSocket / EventSource，
把**同源且属于本应用**的绝对路径改写成带网关前缀的形式：
    /auth/password-login → /app/hermes-desktop/auth/password-login

安全性
------
* 只改**同源**的 URL（跨源的第三方请求保持原样）。
* 只改**以 / 开头**的路径（相对路径由 base 标签负责，无需处理）。
* 已经带前缀的不重复添加。
* 不改 fnOS 自身的保留路径（/cgi-bin、/api/v1 等），避免影响宿主。

参考实现：FNOSP/fnos-dsh 的 packages/fnos-gateway/src/client/bridge.ts
"""

BRIDGE_JS = r"""
(function () {
  "use strict";
  var prefix = "__PREFIX__";
  if (!prefix) return;

  // fnOS 宿主自身的路径，不能改写
  var hostPaths = ["/cgi-bin", "/api/v1", "/api/v2", "/webman", "/desktop"];
  // 属于本应用的路径前缀（其余一律不改写，避免误伤）
  var appPaths = ["/api", "/auth", "/sessions", "/ws", "/assets", "/events", "/sse", "/login"];

  function boundary(pathname, candidate) {
    return pathname === candidate || pathname.indexOf(candidate + "/") === 0;
  }

  function isHostPath(pathname) {
    for (var i = 0; i < hostPaths.length; i++) {
      if (boundary(pathname, hostPaths[i])) return true;
    }
    return false;
  }

  function isAppPath(pathname) {
    for (var i = 0; i < appPaths.length; i++) {
      if (boundary(pathname, appPaths[i])) return true;
    }
    return false;
  }

  function mapUrl(value) {
    var s;
    try { s = String(value); } catch (_) { return null; }
    // 只处理以 / 开头的绝对路径；相对路径交给 base 标签
    if (s.charAt(0) !== "/") return null;
    // 协议相对 URL（//host/path）不动
    if (s.charAt(1) === "/") return null;

    var pathname = s.split("?")[0].split("#")[0];
    if (isHostPath(pathname)) return null;
    if (!isAppPath(pathname)) return null;
    // 已带前缀的不重复加
    if (boundary(pathname, prefix)) return null;
    return prefix + s;
  }

  function mapAny(value) {
    if (typeof value !== "string") return value;
    var mapped = mapUrl(value);
    return mapped === null ? value : mapped;
  }

  // ── fetch ────────────────────────────────────────────────────────────
  var nativeFetch = window.fetch ? window.fetch.bind(window) : null;
  if (nativeFetch) {
    window.fetch = function (input, init) {
      try {
        if (typeof Request !== "undefined" && input instanceof Request) {
          var m1 = mapUrl(input.url);
          if (m1) input = new Request(m1, input);
        } else {
          var m2 = mapUrl(input);
          if (m2) input = m2;
        }
      } catch (_) {}
      return nativeFetch(input, init);
    };
  }

  // ── XMLHttpRequest ───────────────────────────────────────────────────
  try {
    var nativeOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function () {
      var args = Array.prototype.slice.call(arguments);
      var m = mapUrl(args[1]);
      if (m) args[1] = m;
      return nativeOpen.apply(this, args);
    };
  } catch (_) {}

  // ── WebSocket ────────────────────────────────────────────────────────
  try {
    var NativeWS = window.WebSocket;
    if (NativeWS) {
      var WrappedWS = function (url, protocols) {
        var m = mapUrl(url);
        return protocols === undefined ? new NativeWS(m === null ? url : m)
                                      : new NativeWS(m === null ? url : m, protocols);
      };
      WrappedWS.prototype = NativeWS.prototype;
      ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(function (k) {
        try { WrappedWS[k] = NativeWS[k]; } catch (_) {}
      });
      window.WebSocket = WrappedWS;
    }
  } catch (_) {}

  // ── EventSource ──────────────────────────────────────────────────────
  try {
    var NativeES = window.EventSource;
    if (NativeES) {
      var WrappedES = function (url, init) {
        var m = mapUrl(url);
        var u = m === null ? url : m;
        return init === undefined ? new NativeES(u) : new NativeES(u, init);
      };
      WrappedES.prototype = NativeES.prototype;
      window.EventSource = WrappedES;
    }
  } catch (_) {}

  // ── 顶层跳转：location.href / assign / replace ─────────────────────
  // 登录成功后前端会做 location.href = "/" 这类跳转。在 iframe 里绝对路径
  // 会被解析成 fnOS 网关的根路径，表现为「登录后跳到飞牛桌面」。
  // 这里把根相对路径改写成带前缀的。
  function mapLocation(value) {
    if (typeof value !== "string") return value;
    if (value.charAt(0) !== "/") return value;
    if (value.charAt(1) === "/") return value;
    var pathname = value.split("?")[0].split("#")[0];
    if (pathname === "/" || isAppPath(pathname)) return prefix + value;
    return value;
  }

  try {
    var loc = window.location;
    ["assign", "replace"].forEach(function (name) {
      var original = loc[name];
      if (typeof original !== "function") return;
      var wrapped = function (url) {
        var m = mapLocation(url);
        return original.call(this, m === null || m === undefined ? url : m);
      };
      try { loc[name] = wrapped; } catch (_) {}
    });
  } catch (_) {}

  // history.pushState / replaceState 也可能带绝对路径
  try {
    ["pushState", "replaceState"].forEach(function (name) {
      var original = history[name];
      if (typeof original !== "function") return;
      history[name] = function (state, title, url) {
        if (typeof url === "string") {
          var m = mapLocation(url);
          if (m) url = m;
        }
        return original.call(this, state, title, url);
      };
    });
  } catch (_) {}

  // ── 动态插入的 <img>/<script> 的绝对 src ────────────────────────────
  try {
    var nativeSetAttribute = Element.prototype.setAttribute;
    if (nativeSetAttribute) {
      Element.prototype.setAttribute = function (name, value) {
        try {
          var tag = String(this.tagName || "").toUpperCase();
          var attr = String(name || "").toLowerCase();
          var isSrc = (attr === "src" && (tag === "IMG" || tag === "SCRIPT" ||
                    tag === "IFRAME" || tag === "AUDIO" || tag === "VIDEO" ||
                    tag === "SOURCE" || tag === "TRACK" || tag === "INPUT")) ||
                   (attr === "href" && tag === "LINK") ||
                   (attr === "poster" && tag === "VIDEO");
          if (isSrc) {
            var m = mapUrl(value);
            if (m) value = m;
          }
        } catch (_) {}
        return nativeSetAttribute.call(this, name, value);
      };
    }
  } catch (_) {}

  // ── CSS url() ───────────────────────────────────────────────────────
  try {
    var proto = window.CSSStyleDeclaration && window.CSSStyleDeclaration.prototype;
    if (proto && typeof proto.setProperty === "function" &&
        proto.setProperty.__fnosBridge !== true) {
      var originalSetProperty = proto.setProperty;
      var bridged = function (name, value, priority) {
        try {
          if (typeof value === "string" && value.indexOf("url(") >= 0) {
            value = value.replace(/url\(\s*(["']?)(\/(?!\/)[^"')]+)\1\s*\)/gi,
              function (match, quote, path) {
                var m = mapUrl(path);
                return m ? "url(" + quote + m + quote + ")" : match;
              });
          }
        } catch (_) {}
        return originalSetProperty.call(this, name, value, priority);
      };
      try { Object.defineProperty(bridged, "__fnosBridge", { value: true }); } catch (_) {}
      proto.setProperty = bridged;
    }
  } catch (_) {}
})();

  // ── 表单提交：原生 POST 改成 fetch ────────────────────────────────
  // Hermes 登录页是 <form method="POST" action="/auth/password-login">。
  // 原生提交后服务端 302 到 "/"，浏览器在 iframe 里把 "/" 解析成 fnOS
  // 网关根路径 —— 表现就是「登录成功后跳到飞牛桌面」。
  // 浏览器级 302 跳转 JS 拦不住，所以把提交本身改成 fetch，
  // 由我们决定跳回哪里。
  function isAppTarget(action) {
    try {
      var u = new URL(action, location.origin);
      if (u.origin !== location.origin) return false;
      var p = u.pathname;
      return p === "/" || isAppPath(p) || p.indexOf(prefix) === 0;
    } catch (_) {
      return false;
    }
  }

  document.addEventListener("submit", function (ev) {
    var f = ev.target;
    if (!f || String(f.tagName).toUpperCase() !== "FORM") return;
    var action = f.getAttribute("action") || location.pathname;
    if (!isAppTarget(action)) return;

    ev.preventDefault();
    ev.stopPropagation();

    var method = String(f.getAttribute("method") || "GET").toUpperCase();
    var mapped = mapUrl(action);
    var opts = { method: method, credentials: "same-origin" };
    if (method === "POST") opts.body = new FormData(f);

    fetch(mapped === null || mapped === undefined ? action : mapped, opts)
      .then(function (r) {
        if (r.ok) location.replace(prefix + "/");
        else location.reload();
      })
      .catch(function () { location.reload(); });
  }, true);

"""


def build_bridge(prefix: str) -> str:
    """生成带前缀的 bridge 脚本。"""
    return BRIDGE_JS.replace("__PREFIX__", prefix)
