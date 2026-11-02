/**
 * 应用更新 — Hermes Desktop 插件（bundled 版本）
 *
 * 构建时由 import.meta.glob 打进 main bundle（scripts/build.sh 的
 * inject_bundled_plugins() 负责复制到 src/apps/desktop/src/plugins/）。
 *
 * 交互设计：检查 → 下载 → 安装，三步都要求用户主动点击。
 * 安装会让 fnOS 重启应用（会话中断），所以不做静默升级。
 *
 * 纯 ESM、加载时不编译：UI 用 jsx() 调用而非 JSX 语法。
 * 只能 import @hermes/plugin-sdk、react、react/jsx-runtime。
 */

import { Badge, Button, Tip, cn } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'app-updater'
const CHIP_POLL_MS = 300000   // 5 分钟：后端还有 10 分钟缓存，别把 GitHub 限流打满
const JOB_POLL_MS = 900

// ── 小工具 ────────────────────────────────────────────────────────────

function fmtSize(n) {
  if (!n) return ''
  const mb = n / 1048576
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.round(n / 1024)} KB`
}

function fmtDate(iso) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (!t || Number.isNaN(t)) return ''
  const d = new Date(t)
  const diff = Date.now() - t
  const m = Math.floor(diff / 60000)
  if (m < 60) return `${Math.max(1, m)} 分钟前`
  if (m < 1440) return `${Math.floor(m / 60)} 小时前`
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate()
  ).padStart(2, '0')}`
}

// ── 进度条 ────────────────────────────────────────────────────────────

function Bar({ percent }) {
  const p = Math.max(0, Math.min(100, Number(percent) || 0))
  return jsx('div', {
    className: 'h-1.5 w-full overflow-hidden rounded-full bg-(--ui-bg-elevated)',
    children: jsx('div', {
      className: 'h-full rounded-full bg-emerald-500 transition-[width] duration-300',
      style: { width: `${p}%` }
    })
  })
}

// ── 状态栏：仅在「有更新」或「正在处理」时出现 ──────────────────────────

function UpdateChip({ rest }) {
  const [stat, setStat] = useState(null)

  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const r = await rest('/status')
        if (alive) setStat(r || null)
      } catch {
        /* 状态栏探测失败不打扰用户 */
      }
    }
    poll()
    const t = setInterval(poll, CHIP_POLL_MS)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [rest])

  if (!stat) return null
  const phase = (stat.job && stat.job.phase) || 'idle'

  // 常驻显示：无更新时低调显示当前版本（也是「检查更新」的入口），
  // 有更新或正在处理时才点亮，避免平时占视觉注意力。
  let label = stat.current ? `v${stat.current}` : '应用更新'
  let dot = 'bg-(--ui-text-tertiary)'

  if (stat.has_update) {
    label = `可更新 ${stat.latest || ''}`.trim()
    dot = 'bg-amber-500'
  }
  if (phase === 'downloading') {
    label = `下载 ${Math.round(stat.job.percent || 0)}%`
    dot = 'bg-sky-500 animate-pulse'
  } else if (phase === 'verifying') {
    label = '校验中'
    dot = 'bg-sky-500 animate-pulse'
  } else if (phase === 'installing') {
    label = '安装中'
    dot = 'bg-sky-500 animate-pulse'
  } else if (phase === 'done' && stat.job.file) {
    label = '待安装'
    dot = 'bg-emerald-500'
  }

  return jsx(Tip, {
    label: '发现新版本 · 点击查看',
    children: jsx('button', {
      type: 'button',
      onClick: () => {
        location.hash = '#/app-update'
      },
      className: cn(
        'inline-flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem] transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      children: jsxs('span', {
        className: 'inline-flex items-center gap-1',
        children: [
          jsx('span', { className: cn('h-1.5 w-1.5 rounded-full', dot) }),
          jsx('span', { children: label })
        ]
      })
    })
  })
}

// ── 主页面 ────────────────────────────────────────────────────────────

function UpdatePage({ rest }) {
  const [stat, setStat] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const timer = useRef(null)

  const load = useCallback(
    async (refresh) => {
      try {
        const r = await rest(refresh ? '/check' : '/status', refresh ? { method: 'POST' } : undefined)
        setStat(r || null)
        if (r && r.error) setErr(r.error)
      } catch (e) {
        setErr(String((e && e.message) || e))
      }
    },
    [rest]
  )

  useEffect(() => {
    load(false)
  }, [load])

  // 下载/安装期间加密轮询。依赖用阶段字符串而不是整个 stat ——
  // 否则每次取回数据都会重建定时器，进度看起来一顿一顿的。
  const livePhase = (stat && stat.job && stat.job.phase) || 'idle'
  useEffect(() => {
    const active =
      livePhase === 'downloading' || livePhase === 'verifying' || livePhase === 'installing'
    if (!active) return undefined
    const t = setInterval(async () => {
      try {
        const j = await rest('/job')
        setStat((prev) => (prev ? { ...prev, job: j } : prev))
        if (j && j.phase === 'error') setErr(j.error || '出错了')
        if (j && j.phase === 'done' && j.file) setMsg(j.message || '下载完成')
      } catch {
        /* 安装时应用会重启，轮询失败是正常的 */
      }
    }, JOB_POLL_MS)
    return () => clearInterval(t)
  }, [rest, livePhase])

  const act = async (path, label) => {
    setBusy(true)
    setErr('')
    setMsg('')
    try {
      await rest(path, { method: 'POST' })
      setMsg(label)
      await load(false)
    } catch (e) {
      setErr(String((e && e.message) || e))
    } finally {
      setBusy(false)
    }
  }

  if (!stat) {
    return jsx('div', {
      className: 'p-6 text-sm text-(--ui-text-tertiary)',
      children: '读取版本信息…'
    })
  }

  const job = stat.job || { phase: 'idle', percent: 0 }
  const phase = job.phase || 'idle'
  const hasFile = Boolean(job.file)
  const downloading = phase === 'downloading' || phase === 'verifying'
  const installing = phase === 'installing'

  return jsxs('div', {
    className: 'mx-auto w-full max-w-3xl p-6 text-sm',
    children: [
      // 头部
      jsxs('div', {
        className: 'mb-5 flex items-start justify-between gap-4',
        children: [
          jsxs('div', {
            children: [
              jsx('h2', {
                className: 'text-base font-semibold text-foreground',
                children: '应用更新'
              }),
              jsx('p', {
                className: 'mt-1 text-xs text-(--ui-text-tertiary)',
                children: `当前版本 ${stat.current || '未知'}`
              })
            ]
          }),
          jsx('button', {
            type: 'button',
            disabled: busy || downloading || installing,
            onClick: () => load(true),
            className: cn(
              'shrink-0 rounded-md px-2.5 py-1 text-xs transition-colors',
              'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-foreground',
              'disabled:opacity-50'
            ),
            children: '刷新'
          })
        ]
      }),

      // 版本状态卡
      jsxs('div', {
        className:
          'rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) p-4',
        children: [
          jsxs('div', {
            className: 'flex flex-wrap items-center gap-2',
            children: [
              jsx(Badge, {
                variant: stat.has_update ? 'warn' : 'success',
                children: stat.has_update ? '有新版本' : '已是最新'
              }),
              jsx('span', {
                className: 'text-sm text-foreground',
                children: stat.has_update
                  ? `${stat.current} → ${stat.latest}`
                  : stat.current || ''
              }),
              stat.published_at
                ? jsx('span', {
                    className: 'text-xs text-(--ui-text-tertiary)',
                    children: `发布于 ${fmtDate(stat.published_at)}`
                  })
                : null
            ]
          }),

          stat.has_update && stat.asset
            ? jsx('p', {
                className: 'mt-2 text-xs text-(--ui-text-tertiary)',
                children: `${stat.asset.name} · ${fmtSize(stat.asset.size)}`
              })
            : null,

          downloading
            ? jsxs('div', {
                className: 'mt-3 space-y-1.5',
                children: [
                  jsx(Bar, { percent: job.percent }),
                  jsx('p', {
                    className: 'text-xs text-(--ui-text-tertiary)',
                    children:
                      phase === 'verifying'
                        ? '校验完整性…'
                        : `下载中 ${Math.round(job.percent || 0)}%${job.via ? ` · ${job.via}` : ''}`
                  })
                ]
              })
            : null,

          installing
            ? jsx('p', {
                className: 'mt-3 text-xs text-sky-600 dark:text-sky-300',
                children: '已交给应用中心安装，应用即将重启（约 2–5 分钟）。页面会断开，属正常。'
              })
            : null,

          // 操作区
          jsxs('div', {
            className: 'mt-4 flex flex-wrap items-center gap-2',
            children: [
              stat.has_update && !hasFile && !downloading && !installing
                ? jsx(Button, {
                    variant: 'default',
                    disabled: busy,
                    onClick: () => act('/download', '开始下载…'),
                    children: '下载更新'
                  })
                : null,

              hasFile && !installing
                ? jsx(Button, {
                    variant: 'default',
                    disabled: busy || !stat.authorized,
                    onClick: () => act('/install', '已发起安装…'),
                    children: '立即安装并重启'
                  })
                : null,

              hasFile && !installing
                ? jsx(Button, {
                    variant: 'outline',
                    disabled: busy,
                    onClick: () => act('/clean', '已清除缓存'),
                    children: '清除安装包'
                  })
                : null,

              stat.has_update && stat.html_url
                ? jsx('a', {
                    href: stat.html_url,
                    target: '_blank',
                    rel: 'noreferrer',
                    className: 'text-xs text-(--ui-text-tertiary) underline hover:text-foreground',
                    children: '在 GitHub 上查看'
                  })
                : null
            ]
          }),

          // 未授权提示
          stat.authorized === false
            ? jsx('p', {
                className: 'mt-3 text-xs text-amber-600 dark:text-amber-400',
                children:
                  'trim-cli 未授权，无法调用应用中心安装。请先在「fnOS 授权」里给本机授权，或手动下载 fpk 安装。'
              })
            : null
        ]
      }),

      // 更新说明
      stat.notes
        ? jsxs('div', {
            className: 'mt-4',
            children: [
              jsx('h3', {
                className: 'mb-2 text-xs font-medium text-(--ui-text-secondary)',
                children: '更新说明'
              }),
              jsx('pre', {
                className:
                  'max-h-80 overflow-auto whitespace-pre-wrap rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) p-3 font-sans text-xs leading-relaxed text-(--ui-text-secondary)',
                children: stat.notes
              })
            ]
          })
        : null,

      // 提示
      msg
        ? jsx('p', { className: 'mt-3 text-xs text-emerald-600 dark:text-emerald-400', children: msg })
        : null,
      err || stat.error
        ? jsx('p', { className: 'mt-3 text-xs text-red-600 dark:text-red-400', children: err || stat.error })
        : null
    ]
  })
}

// ── 插件定义 ──────────────────────────────────────────────────────────

export default {
  id: ID,
  name: '应用更新',
  description: '检查并升级 Hermes Desktop 的 fpk',
  register(ctx) {
    ctx.register({
      area: 'routes',
      id: 'app-updater:page',
      title: '应用更新',
      data: { path: '/app-update' },
      render: () => jsx(UpdatePage, { rest: ctx.rest })
    })

    ctx.register({
      area: 'statusBar.right',
      id: 'app-updater:chip',
      order: 130,
      render: () => jsx(UpdateChip, { rest: ctx.rest })
    })
  }
}
