/**
 * fnOS 授权 — Hermes Desktop 插件（runtime door 版本）
 *
 * 放置位置（二选一，目录名必须等于 id）：
 *   <hermes home>/desktop-plugins/fnos-auth/plugin.js
 *   <hermes home>/plugins/fnos-auth/desktop/plugin.js   ← 本应用采用（统一包的另一半）
 *
 * 为什么需要这个文件：
 * 原插件只提供 `dashboard/dist/index.js`，它用的是旧 API
 * `window.__HERMES_PLUGINS__.register` —— 当前 webapp 前端根本没有这个全局，
 * 于是脚本第一行 `if (!SDK) return` 就静默返回，tab 永远不出现。
 * 现在的前端走 contributions 机制：default export 一个 HermesPlugin，
 * 在 register(ctx) 里用 ctx.register({ area, ... }) 挂载界面。
 *
 * 纯 ESM，加载时不编译 —— UI 用 jsx() 调用而非 JSX 语法。
 * 只有这些 import 能解析：@hermes/plugin-sdk、react、react/jsx-runtime。
 *
 * 后端在同目录的 plugin_api.py（/api/plugins/fnos-auth/*），
 * 通过 ctx.rest() 访问，它自动加上本插件的命名空间。
 */

import { jsx, jsxs } from 'react/jsx-runtime'
import { useCallback, useEffect, useState } from 'react'

const ID = 'fnos-auth'

// ── 小组件 ────────────────────────────────────────────────────────────

function Field({ label, children }) {
  return jsxs('label', {
    className: 'flex flex-col gap-1',
    children: [
      jsx('span', {
        className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
        children: label
      }),
      children
    ]
  })
}

const inputCls =
  'h-8 rounded-md border border-(--ui-border) bg-(--ui-bg-elevated) px-2 text-sm ' +
  'text-foreground outline-none focus:border-(--ui-accent)'

const btnCls =
  'inline-flex h-7 items-center gap-1 rounded-md border border-(--ui-border) px-2 ' +
  'text-xs text-foreground transition-colors hover:bg-(--chrome-action-hover) ' +
  'disabled:cursor-not-allowed disabled:opacity-40'

const btnPrimary =
  'inline-flex h-7 items-center gap-1 rounded-md px-2 text-xs font-medium ' +
  'text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed ' +
  'disabled:opacity-40'

// ── 主页面 ────────────────────────────────────────────────────────────

function FnosAuthPage({ rest }) {
  const [devices, setDevices] = useState([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState('')
  // device_id -> { url, code, msg }
  const [flow, setFlow] = useState({})

  const patchFlow = useCallback((id, patch) => {
    setFlow(prev => ({ ...prev, [id]: { ...(prev[id] || {}), ...patch } }))
  }, [])

  const load = useCallback(async () => {
    try {
      setErr('')
      const r = await rest('/devices')
      setDevices(Array.isArray(r) ? r : (r && r.devices) || [])
    } catch (e) {
      setErr(String((e && e.message) || e))
    } finally {
      setLoading(false)
    }
  }, [rest])

  useEffect(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [load])

  async function addDevice() {
    const t = target.trim()
    if (!t) return
    setBusy('add')
    try {
      await rest('/devices', { method: 'POST', body: { target: t } })
      setTarget('')
      await load()
    } catch (e) {
      setErr(String((e && e.message) || e))
    } finally {
      setBusy('')
    }
  }

  async function startAuth(dev) {
    setBusy(dev.id)
    patchFlow(dev.id, { msg: '' })
    try {
      const r = await rest(`/devices/${dev.id}/start`, { method: 'POST' })
      patchFlow(dev.id, { url: (r && (r.url || r.auth_url)) || '' })
    } catch (e) {
      patchFlow(dev.id, { msg: String((e && e.message) || e) })
    } finally {
      setBusy('')
    }
  }

  async function submitCode(dev) {
    const code = ((flow[dev.id] || {}).code || '').trim()
    if (!code) return
    setBusy(dev.id)
    try {
      await rest(`/devices/${dev.id}/code`, { method: 'POST', body: { code } })
      patchFlow(dev.id, { msg: '授权成功', url: '', code: '' })
      await load()
    } catch (e) {
      patchFlow(dev.id, { msg: String((e && e.message) || e) })
    } finally {
      setBusy('')
    }
  }

  async function logout(dev) {
    setBusy(dev.id)
    try {
      await rest(`/devices/${dev.id}/logout`, { method: 'POST' })
      patchFlow(dev.id, { msg: '已撤销' })
      await load()
    } catch (e) {
      patchFlow(dev.id, { msg: String((e && e.message) || e) })
    } finally {
      setBusy('')
    }
  }

  async function remove(dev) {
    setBusy(dev.id)
    try {
      await rest(`/devices/${dev.id}/delete`, { method: 'POST' })
      await load()
    } catch (e) {
      patchFlow(dev.id, { msg: String((e && e.message) || e) })
    } finally {
      setBusy('')
    }
  }

  // ── 渲染 ──

  const header = jsxs('div', {
    className: 'flex items-baseline gap-2',
    children: [
      jsx('h1', { className: 'text-base font-medium', children: 'fnOS 授权' }),
      jsx('span', {
        className: 'text-xs text-(--ui-text-tertiary)',
        children: '给 Agent 开通飞牛 NAS 的操作权限'
      })
    ]
  })

  const addRow = jsxs('div', {
    className: 'flex items-end gap-2',
    children: [
      jsxs('div', {
        className: 'flex-1',
        children: jsx(Field, {
          label: '设备地址',
          children: jsx('input', {
            className: inputCls + ' w-full',
            placeholder: '192.168.1.28 或 nas.example.com:5666',
            value: target,
            onChange: e => setTarget(e.target.value),
            onKeyDown: e => {
              if (e.key === 'Enter') addDevice()
            }
          })
        })
      }),
      jsx('button', {
        className: btnCls,
        type: 'button',
        disabled: busy === 'add' || !target.trim(),
        onClick: addDevice,
        children: busy === 'add' ? '添加中…' : '添加设备'
      })
    ]
  })

  function renderDevice(dev) {
    const f = flow[dev.id] || {}
    const authed = !!(dev.authorized || dev.authed)
    const working = busy === dev.id

    return jsxs('div', {
      className: 'rounded-lg border border-(--ui-border) p-3',
      children: [
        jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx('span', { className: 'text-sm font-medium', children: dev.name || dev.target }),
            jsx('span', {
              className: 'rounded px-1.5 py-0.5 text-[0.625rem] ' +
                (authed
                  ? 'bg-(--ui-success-bg, rgba(34,197,94,.15)) text-green-600'
                  : 'bg-(--ui-bg-elevated) text-(--ui-text-tertiary)'),
              children: authed ? '已授权' : '未授权'
            }),
            jsx('span', {
              className: 'flex-1 truncate text-xs text-(--ui-text-tertiary)',
              children: dev.base || dev.target
            })
          ]
        }),

        f.url
          ? jsxs('div', {
            className: 'mt-2 flex flex-col gap-1 rounded-md bg-(--ui-bg-elevated) p-2',
            children: [
              jsx('div', {
                className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                children: '在浏览器打开下面的链接完成登录，然后把授权码粘到下面：'
              }),
              jsx('a', {
                href: f.url,
                target: '_blank',
                rel: 'noreferrer',
                className: 'break-all text-xs underline',
                children: f.url
              })
            ]
          })
          : null,

        jsxs('div', {
          className: 'mt-2 flex items-center gap-2',
          children: [
            jsx('input', {
              className: inputCls + ' flex-1',
              placeholder: '粘贴授权码',
              value: f.code || '',
              onChange: e => patchFlow(dev.id, { code: e.target.value }),
              onKeyDown: e => {
                if (e.key === 'Enter') submitCode(dev)
              }
            }),
            jsx('button', {
              className: btnPrimary,
              style: { background: 'var(--ui-accent, #2563eb)' },
              type: 'button',
              disabled: working || !(f.code || '').trim(),
              onClick: () => submitCode(dev),
              children: working ? '提交中…' : '提交'
            }),
            jsx('button', {
              className: btnCls,
              type: 'button',
              disabled: working,
              onClick: () => startAuth(dev),
              children: f.url ? '重新获取链接' : '发起授权'
            }),
            authed
              ? jsx('button', {
                className: btnCls,
                type: 'button',
                disabled: working,
                onClick: () => logout(dev),
                children: '撤销'
              })
              : null,
            jsx('button', {
              className: btnCls,
              type: 'button',
              disabled: working,
              onClick: () => remove(dev),
              children: '移除'
            })
          ]
        }),

        f.msg
          ? jsx('div', {
            className: 'mt-1 text-xs text-(--ui-text-tertiary)',
            children: f.msg
          })
          : null
      ]
    })
  }

  const body = loading
    ? jsx('div', { className: 'text-sm text-(--ui-text-tertiary)', children: '加载中…' })
    : devices.length === 0
      ? jsx('div', {
        className: 'rounded-lg border border-dashed border-(--ui-border) p-6 text-center text-sm text-(--ui-text-tertiary)',
        children: '还没有设备。在上面填入 NAS 地址添加一台。'
      })
      : jsx('div', {
        className: 'flex flex-col gap-2',
        children: devices.map(renderDevice)
      })

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-auto p-4 text-sm',
    children: [
      header,
      addRow,
      err
        ? jsx('div', {
          className: 'rounded-md bg-(--ui-bg-elevated) p-2 text-xs text-red-500',
          children: err
        })
        : null,
      body,
      jsx('div', {
        className: 'mt-auto pt-2 text-[0.6875rem] text-(--ui-text-tertiary)',
        children:
          '授权后 Agent 即可操作该设备（文件、Docker、应用中心、存储、监控等）。' +
          '令牌保存在本机，可在上面的「撤销」按钮随时收回。'
      })
    ]
  })
}

// ── 插件定义 ──────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'fnOS 授权',
  description: '给 Agent 开通飞牛 NAS 的操作权限：填地址 → 授权 → 粘贴授权码',
  register(ctx) {
    ctx.register({
      area: 'routes',
      id: 'fnos-auth:page',
      title: 'fnOS 授权',
      data: { path: '/fnos-auth' },
      render: () => jsx(FnosAuthPage, { rest: ctx.rest })
    })
  }
}
