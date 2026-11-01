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
"""


def build_bridge(prefix: str) -> str:
    """生成带前缀的 bridge 脚本。"""
    return BRIDGE_JS.replace("__PREFIX__", prefix)
