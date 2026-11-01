/**
 * fnOS 授权 — Hermes Desktop 插件（bundled 版本）
 *
 * 由 vite 的 import.meta.glob 在构建时打进 main bundle：
 *   src/apps/desktop/src/plugins/<id>/plugin.{js,tsx}
 * 构建脚本 scripts/build.sh 的 inject_bundled_plugins() 负责把本文件
 * 从 packaging/fpk/app/plugins/fnos-auth/desktop/plugin.js 复制过去。
 *
 * 注意：<hermes home>/desktop-plugins/<id>/plugin.js 这条 runtime door
 * 依赖 window.hermesDesktop（Electron 专属 IPC），纯 webapp 下不可用，
 * 所以浏览器版必须走 bundled 这条路。
 *
 * 纯 ESM、加载时不编译：UI 用 jsx() 调用而非 JSX 语法。
 * 只能 import @hermes/plugin-sdk、react、react/jsx-runtime。
 */

import { Badge, Button, Input, Tip, cn } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'fnos-auth'

const POLL_MS = 15000
const COOLDOWN_MS = 2500

// ── 小工具 ────────────────────────────────────────────────────────────

function hostOf(d) {
  if (!d) return ''
  const scheme = d.scheme || 'http'
  const host = d.host || d.target || ''
  const port = d.port ? `:${d.port}` : ''
  return `${scheme}://${host}${port}`
}

/** 把接口给的时间戳格式化成「3 分钟前」。 */
function ago(ts) {
  if (!ts) return ''
  const t = typeof ts === 'number' ? (ts < 1e12 ? ts * 1000 : ts) : Date.parse(ts)
  if (!t || Number.isNaN(t)) return ''
  const diff = Date.now() - t
  if (diff < 0) return '刚刚'
  const m = Math.floor(diff / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}

// ── 内联提示条 ────────────────────────────────────────────────────────

function Notice({ kind, children, onClose }) {
  const tone =
    kind === 'error'
      ? 'border-red-500/30 bg-red-500/10 text-red-600 dark:text-red-300'
      : kind === 'success'
        ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300'
        : 'border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) text-(--ui-text-secondary)'
  return jsxs('div', {
    className: cn('flex items-start gap-2 rounded-lg border px-3 py-2 text-xs', tone),
    children: [
      jsx('div', { className: 'flex-1 leading-relaxed', children }),
      onClose
        ? jsx('button', {
            type: 'button',
            onClick: onClose,
            className: 'shrink-0 opacity-60 hover:opacity-100',
            children: '✕'
          })
        : null
    ]
  })
}

// ── 单台设备卡片 ──────────────────────────────────────────────────────

function DeviceCard({ dev, rest, onChanged, onToast }) {
  const [busy, setBusy] = useState('')
  const [url, setUrl] = useState('')
  const [code, setCode] = useState('')
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [msg, setMsg] = useState(null)
  const [cool, setCool] = useState(false)
  const timer = useRef(null)

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  const authed = !!dev.authorized
  const base = hostOf(dev)
  const lastAt = dev.authorized_at || dev.authorizedAt || dev.last_authorized

  async function guard(key, fn) {
    if (busy) return
    setBusy(key)
    setMsg(null)
    try {
      await fn()
    } catch (e) {
      setMsg({ kind: 'error', text: String((e && e.message) || e) })
    } finally {
      setBusy('')
    }
  }

  function startCooldown() {
    setCool(true)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setCool(false), COOLDOWN_MS)
  }

  const start = () =>
    guard('start', async () => {
      // 每次发起都会作废上一个 client_id，冷却期内不重复请求
      const r = await rest(`/devices/${dev.id}/start`, { method: 'POST' })
      const u = (r && (r.url || r.auth_url)) || ''
      setUrl(u)
      startCooldown()
      if (!u) setMsg({ kind: 'error', text: '后端没有返回授权链接' })
    })

  const submit = () =>
    guard('submit', async () => {
      const c = code.trim()
      if (!c) return
      await rest(`/devices/${dev.id}/code`, { method: 'POST', body: { code: c } })
      setCode('')
      setUrl('')
      setMsg({ kind: 'success', text: '授权成功' })
      onChanged()
      onToast('授权成功')
    })

  const logout = () =>
    guard('logout', async () => {
      await rest(`/devices/${dev.id}/logout`, { method: 'POST' })
      setMsg({ kind: 'info', text: '已撤销该设备的授权' })
      onChanged()
    })

  const remove = () =>
    guard('remove', async () => {
      await rest(`/devices/${dev.id}/delete`, { method: 'POST' })
      onChanged()
      onToast('设备已移除')
    })

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(url)
      onToast('链接已复制')
    } catch {
      onToast('复制失败，请手动选中')
    }
  }

  const header = jsxs('div', {
    className: 'flex items-center gap-2.5',
    children: [
      jsx('span', {
        className: cn(
          'h-2 w-2 shrink-0 rounded-full',
          authed ? 'bg-emerald-500' : 'bg-(--ui-text-tertiary)'
        )
      }),
      jsx('span', {
        className: 'truncate text-sm font-medium text-(--ui-text-primary)',
        children: dev.name || dev.target
      }),
      authed
        ? jsx(Badge, { variant: 'success', size: 'xs', children: '已授权' })
        : jsx(Badge, { variant: 'muted', size: 'xs', children: '未授权' }),
      dev.builtin ? jsx(Badge, { variant: 'outline', size: 'xs', children: '本机' }) : null,
      jsx('span', { className: 'flex-1' }),
      lastAt
        ? jsx('span', {
            className: 'shrink-0 text-[0.6875rem] text-(--ui-text-tertiary)',
            children: `授权于 ${ago(lastAt)}`
          })
        : null
    ]
  })

  const meta = jsxs('div', {
    className: 'mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 pl-[1.125rem] text-[0.6875rem] text-(--ui-text-tertiary)',
    children: [
      jsx('code', { className: 'font-mono', children: base }),
      dev.user ? jsx('span', { children: `· 用户 ${dev.user}` }) : null,
      dev.profile ? jsx('span', { children: `· profile ${dev.profile}` }) : null
    ]
  })

  const linkBox = url
    ? jsxs('div', {
        className: 'mt-3 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-primary)/60 p-3',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', {
                className: 'text-[0.6875rem] font-medium text-(--ui-text-secondary)',
                children: '打开链接完成登录，再把授权码粘到下面'
              }),
              jsx('span', { className: 'flex-1' }),
              jsx(Button, {
                variant: 'text',
                size: 'sm',
                type: 'button',
                onClick: copyLink,
                children: '复制链接'
              })
            ]
          }),
          jsx('a', {
            href: url,
            target: '_blank',
            rel: 'noreferrer',
            className: 'mt-1.5 block break-all font-mono text-[0.6875rem] leading-relaxed text-primary underline-offset-2 hover:underline',
            children: url
          }),
          jsx('div', {
            className: 'mt-1.5 text-[0.625rem] text-(--ui-text-tertiary)',
            children: '再次点「重新授权」会作废上一个链接，请只用一个。'
          })
        ]
      })
    : null

  const actions = jsxs('div', {
    className: 'mt-2.5 flex flex-wrap items-center gap-2 pl-[1.125rem]',
    children: [
      jsx(Input, {
        containerClassName: 'min-w-[14rem] flex-1 max-w-[22rem]',
        size: 'sm',
        placeholder: authed ? '更换账号时粘贴新的授权码' : '粘贴授权码',
        value: code,
        onChange: e => setCode(e.target.value),
        onKeyDown: e => {
          if (e.key === 'Enter') submit()
        }
      }),
      jsx(Button, {
        variant: 'default',
        size: 'sm',
        type: 'button',
        disabled: busy === 'submit' || !code.trim(),
        onClick: submit,
        children: busy === 'submit' ? '提交中…' : '提交'
      }),
      jsx(Button, {
        variant: 'secondary',
        size: 'sm',
        type: 'button',
        disabled: busy === 'start' || cool,
        onClick: start,
        children: busy === 'start' ? '获取中…' : cool ? '稍候…' : url ? '重新授权' : '发起授权'
      }),
      authed
        ? jsx(Button, {
            variant: 'text',
            size: 'sm',
            type: 'button',
            disabled: !!busy,
            onClick: logout,
            children: '撤销'
          })
        : null,
      jsx('span', { className: 'flex-1' }),
      dev.builtin
        ? null
        : jsx(Button, {
            variant: confirmRemove ? 'destructive' : 'text',
            size: 'sm',
            type: 'button',
            disabled: !!busy,
            onClick: () => {
              if (confirmRemove) {
                remove()
              } else {
                setConfirmRemove(true)
                setTimeout(() => setConfirmRemove(false), 3000)
              }
            },
            children: confirmRemove ? '确认移除？' : '移除'
          })
    ]
  })

  const msgLine = msg
    ? jsx('div', {
        className: 'mt-2 pl-[1.125rem]',
        children: jsx(Notice, { kind: msg.kind, children: msg.text })
      })
    : null

  return jsxs('div', {
    className: cn(
      'rounded-xl border p-3.5 transition-colors',
      'border-(--ui-stroke-secondary) bg-(--ui-bg-elevated)/40'
    ),
    children: [header, meta, linkBox, actions, msgLine]
  })
}

// ── 主页面 ────────────────────────────────────────────────────────────

function FnosAuthPage({ rest }) {
  const [devices, setDevices] = useState([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [target, setTarget] = useState('')
  const [adding, setAdding] = useState(false)
  const [toast, setToast] = useState('')
  const toastTimer = useRef(null)

  const showToast = useCallback(text => {
    setToast(text)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(''), 2200)
  }, [])

  const load = useCallback(async () => {
    try {
      setErr('')
      const r = await rest('/devices')
      const list = Array.isArray(r) ? r : (r && r.devices) || []
      setDevices(list)
    } catch (e) {
      setErr(String((e && e.message) || e))
    } finally {
      setLoading(false)
    }
  }, [rest])

  useEffect(() => {
    load()
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [load])

  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current) }, [])

  async function addDevice() {
    const t = target.trim()
    if (!t || adding) return
    setAdding(true)
    setErr('')
    try {
      await rest('/devices', { method: 'POST', body: { target: t } })
      setTarget('')
      await load()
      showToast('设备已添加')
    } catch (e) {
      setErr(String((e && e.message) || e))
    } finally {
      setAdding(false)
    }
  }

  const stats = useMemo(() => {
    const total = devices.length
    const ok = devices.filter(d => d.authorized).length
    return { total, ok }
  }, [devices])

  const header = jsxs('div', {
    className: 'flex items-start gap-3',
    children: [
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsx('h1', {
            className: 'text-base font-semibold text-(--ui-text-primary)',
            children: 'fnOS 授权'
          }),
          jsx('p', {
            className: 'text-xs leading-relaxed text-(--ui-text-secondary)',
            children:
              '给 Agent 开通飞牛 NAS 的操作权限。授权后它能读写文件、管理 Docker 与应用、查看存储与监控。'
          })
        ]
      }),
      jsx('span', { className: 'flex-1' }),
      stats.total
        ? jsxs('div', {
            className: 'flex shrink-0 items-center gap-1.5',
            children: [
              jsx(Badge, { variant: 'muted', size: 'xs', children: `${stats.total} 台设备` }),
              jsx(Badge, {
                variant: stats.ok ? 'success' : 'muted',
                size: 'xs',
                children: `${stats.ok} 台已授权`
              })
            ]
          })
        : null
    ]
  })

  const addBox = jsxs('div', {
    className: 'rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated)/40 p-3',
    children: [
      jsx('div', {
        className: 'mb-2 text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary)',
        children: '添加设备'
      }),
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Input, {
            containerClassName: 'flex-1',
            size: 'sm',
            placeholder: '192.168.1.28 或 nas.example.com:5666',
            value: target,
            onChange: e => setTarget(e.target.value),
            onKeyDown: e => {
              if (e.key === 'Enter') addDevice()
            }
          }),
          jsx(Button, {
            variant: 'default',
            size: 'sm',
            type: 'button',
            disabled: adding || !target.trim(),
            onClick: addDevice,
            children: adding ? '添加中…' : '添加'
          })
        ]
      })
    ]
  })

  const body = loading
    ? jsx('div', {
        className: 'py-10 text-center text-sm text-(--ui-text-tertiary)',
        children: '加载中…'
      })
    : devices.length === 0
      ? jsxs('div', {
          className: 'rounded-xl border border-dashed border-(--ui-stroke-secondary) px-6 py-10 text-center',
          children: [
            jsx('div', {
              className: 'text-sm font-medium text-(--ui-text-secondary)',
              children: '还没有设备'
            }),
            jsx('div', {
              className: 'mt-1 text-xs leading-relaxed text-(--ui-text-tertiary)',
              children: '在上面填入 NAS 的地址添加一台，然后点「发起授权」按提示完成登录。'
            })
          ]
        })
      : jsx('div', {
          className: 'flex flex-col gap-2.5',
          children: devices.map(d =>
            jsx(DeviceCard, { dev: d, rest, onChanged: load, onToast: showToast }, d.id)
          )
        })

  return jsxs('div', {
    className: 'relative flex h-full flex-col gap-3.5 overflow-auto p-5',
    children: [
      header,
      err ? jsx(Notice, { kind: 'error', children: err, onClose: () => setErr('') }) : null,
      addBox,
      body,
      jsx('div', {
        className: 'pt-1 text-[0.625rem] leading-relaxed text-(--ui-text-tertiary)',
        children:
          '授权令牌保存在本机，随时可用「撤销」收回。Agent 只能操作你显式授权的设备。'
      }),
      toast
        ? jsx('div', {
            className: cn(
              'pointer-events-none sticky bottom-0 mx-auto w-fit rounded-full px-3 py-1.5 text-xs',
              'bg-(--ui-text-primary) text-(--ui-bg-primary) shadow-lg'
            ),
            children: toast
          })
        : null
    ]
  })
}


// ── 状态栏入口 ────────────────────────────────────────────────────────
// 这个前端版本的侧边栏是固定的（contributions 的 area 只有 layouts / panes /
// routes / statusBar.* / titleBar.*，没有 nav/sidebar），插件无法往左侧导航
// 插入口。所以入口放右下角状态栏：本来就放状态与工具，不占主界面空间，
// 还能顺带显示「已授权/总数」当作状态指示。

function FnosAuthChip({ rest }) {
  const [stat, setStat] = useState(null)

  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const r = await rest('/devices')
        const list = Array.isArray(r) ? r : (r && r.devices) || []
        if (alive) {
          setStat({ total: list.length, ok: list.filter(d => d.authorized).length })
        }
      } catch {
        /* 状态栏里的探测失败不打扰用户 */
      }
    }
    poll()
    const t = setInterval(poll, 60000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [rest])

  const label = stat && stat.total ? `fnOS ${stat.ok}/${stat.total}` : 'fnOS 授权'
  const warn = stat && stat.total > 0 && stat.ok < stat.total

  return jsx(Tip, {
    label: 'fnOS 授权 · 管理 Agent 的 NAS 访问权限',
    children: jsx('button', {
      type: 'button',
      onClick: () => {
        // 路由已由下面的 area:'routes' 注册，改 hash 即可切过去
        location.hash = '#/fnos-auth'
      },
      className: cn(
        'inline-flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem] transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      children: jsxs('span', {
        className: 'inline-flex items-center gap-1',
        children: [
          jsx('span', {
            className: cn(
              'h-1.5 w-1.5 rounded-full',
              warn ? 'bg-amber-500' : 'bg-emerald-500'
            )
          }),
          jsx('span', { children: label })
        ]
      })
    })
  })
}

// ── 插件定义 ──────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'fnOS 授权',
  description: '给 Agent 开通飞牛 NAS 的操作权限',
  register(ctx) {
    // 页面路由（直接访问 #/fnos-auth 也能打开）
    ctx.register({
      area: 'routes',
      id: 'fnos-auth:page',
      title: 'fnOS 授权',
      data: { path: '/fnos-auth' },
      render: () => jsx(FnosAuthPage, { rest: ctx.rest })
    })

    // 右下角状态栏入口 + 授权进度指示
    ctx.register({
      area: 'statusBar.right',
      id: 'fnos-auth:chip',
      order: 120,
      render: () => jsx(FnosAuthChip, { rest: ctx.rest })
    })
  }
}
