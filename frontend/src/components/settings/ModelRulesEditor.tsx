/**
 * 按模型注入规则编辑器：结构化表单 + JSON 编辑双模式
 * - 默认：规则卡片 + 上下排序 + 编辑/删除
 * - JSON 模式：保留原 textarea 批量编辑能力
 */
import { useState } from 'react'
import { Plus, Pencil, Trash2, ChevronUp, ChevronDown, FileJson, List, AlertTriangle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { useI18n } from '@/lib/i18n'
import { toast } from '@/lib/toast'
import type { ModelRule } from '@/types/api'

interface ModelRulesEditorProps {
  value: ModelRule[]
  onChange: (rules: ModelRule[]) => void
}

// 凭证头黑名单（复用 Settings.tsx 的逻辑）
const CREDENTIAL_HEADERS = [
  'authorization',
  'x-api-key',
  'api-key',
  'apikey',
  'x-auth-token',
  'auth-token',
  'cookie',
  'x-session',
  'session-id',
]
const isCredentialHeader = (k: string) => CREDENTIAL_HEADERS.includes(k.toLowerCase())
const PLACEHOLDER_HEADER_VAL = /^\s*(?:Bearer\s+)?<[^<>]{1,64}>\s*$/i

export function ModelRulesEditor({ value, onChange }: ModelRulesEditorProps) {
  const { t, tf } = useI18n()
  const [mode, setMode] = useState<'form' | 'json'>('form')
  const [jsonText, setJsonText] = useState('')
  const [editorOpen, setEditorOpen] = useState(false)
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [deletingIndex, setDeletingIndex] = useState<number | null>(null)

  // 切换到 JSON 模式时，把表单数据序列化
  const switchToJson = () => {
    setJsonText(JSON.stringify(value, null, 2))
    setMode('json')
  }

  // 切换回表单模式时，解析 JSON
  const switchToForm = () => {
    if (!jsonText.trim()) {
      onChange([])
      setMode('form')
      return
    }
    try {
      const parsed = JSON.parse(jsonText)
      if (!Array.isArray(parsed)) {
        toast(t('settings.modelRules.jsonInvalid'), 'error')
        return
      }
      // 简单校验
      for (const rule of parsed) {
        if (!rule || typeof rule.match !== 'string') {
          toast(t('settings.modelRules.jsonInvalid'), 'error')
          return
        }
      }
      onChange(parsed)
      setMode('form')
    } catch {
      toast(t('settings.modelRules.jsonInvalid'), 'error')
    }
  }

  // 格式化 JSON
  const formatJson = () => {
    try {
      const parsed = JSON.parse(jsonText)
      setJsonText(JSON.stringify(parsed, null, 2))
    } catch {
      toast(t('settings.modelRules.jsonInvalid'), 'error')
    }
  }

  // 上移
  const moveUp = (idx: number) => {
    if (idx === 0) return
    const newRules = [...value]
    ;[newRules[idx - 1], newRules[idx]] = [newRules[idx], newRules[idx - 1]]
    onChange(newRules)
  }

  // 下移
  const moveDown = (idx: number) => {
    if (idx === value.length - 1) return
    const newRules = [...value]
    ;[newRules[idx], newRules[idx + 1]] = [newRules[idx + 1], newRules[idx]]
    onChange(newRules)
  }

  // 删除
  const deleteRule = (idx: number) => {
    const newRules = value.filter((_, i) => i !== idx)
    onChange(newRules)
    setDeletingIndex(null)
  }

  const openEditor = (idx: number | null) => {
    setEditingIndex(idx)
    setEditorOpen(true)
  }

  const closeEditor = () => {
    setEditorOpen(false)
    setEditingIndex(null)
  }

  const saveRule = (rule: ModelRule) => {
    if (editingIndex === null) {
      // 新增
      onChange([...value, rule])
    } else {
      // 编辑
      const newRules = [...value]
      newRules[editingIndex] = rule
      onChange(newRules)
    }
    closeEditor()
  }

  if (mode === 'json') {
    return (
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label className="text-[11px]">{t('settings.modelRules.jsonMode')}</Label>
          <div className="flex gap-1.5">
            <Button size="sm" variant="ghost" className="h-7 text-[11px]" onClick={formatJson}>
              {t('settings.modelRules.format')}
            </Button>
            <Button size="sm" variant="outline" className="h-7 text-[11px]" onClick={switchToForm}>
              <List className="mr-1 h-3 w-3" />
              {t('settings.modelRules.switchToForm')}
            </Button>
          </div>
        </div>
        <Textarea
          className="min-h-52 font-mono text-[11px]"
          value={jsonText}
          onChange={(e) => setJsonText(e.target.value)}
          placeholder={t('settings.upstream.modelRulesPh')}
        />
        <p className="text-[11px] text-muted-foreground">{t('settings.upstream.modelRulesHint')}</p>
      </div>
    )
  }

  // 表单模式
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label className="text-[11px]">
          {t('settings.modelRules.title')} ({value.length} {t('settings.modelRules.countUnit')})
        </Label>
        <div className="flex gap-1.5">
          <Button size="sm" variant="outline" className="h-7 text-[11px]" onClick={() => openEditor(null)}>
            <Plus className="mr-1 h-3 w-3" />
            {t('settings.modelRules.add')}
          </Button>
          <Button size="sm" variant="ghost" className="h-7 text-[11px]" onClick={switchToJson}>
            <FileJson className="mr-1 h-3 w-3" />
            {t('settings.modelRules.jsonEdit')}
          </Button>
        </div>
      </div>

      {value.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed py-8 text-center">
          <List className="h-8 w-8 text-muted-foreground/40" />
          <p className="text-[11px] text-muted-foreground">{t('settings.modelRules.empty')}</p>
          <p className="text-[11px] text-muted-foreground">{t('settings.modelRules.emptyHint')}</p>
        </div>
      ) : (
        <div className="space-y-1.5">
          {value.map((rule, idx) => (
            <div key={idx} className="group flex items-center gap-2 rounded-lg border bg-card/60 p-2.5">
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex items-center gap-2">
                  <Badge variant="outline" className="shrink-0 text-[10px]">
                    {tf('settings.modelRules.ruleNum', { n: idx + 1 })}
                  </Badge>
                  <span className="truncate font-mono text-[11px]">{rule.match}</span>
                </div>
                <div className="flex gap-2 text-[10px] text-muted-foreground">
                  <span>
                    {t('settings.modelRules.headersLabel')}: {Object.keys(rule.headers || {}).length}
                  </span>
                  <span>·</span>
                  <span>
                    {t('settings.modelRules.bodyLabel')}: {Object.keys(rule.body || {}).length}
                  </span>
                </div>
              </div>
              <div className="flex shrink-0 gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 w-7 p-0"
                  onClick={() => moveUp(idx)}
                  disabled={idx === 0}
                  title={t('settings.modelRules.moveUp')}
                >
                  <ChevronUp className="h-3.5 w-3.5" />
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 w-7 p-0"
                  onClick={() => moveDown(idx)}
                  disabled={idx === value.length - 1}
                  title={t('settings.modelRules.moveDown')}
                >
                  <ChevronDown className="h-3.5 w-3.5" />
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 w-7 p-0"
                  onClick={() => openEditor(idx)}
                  title={t('settings.modelRules.edit')}
                >
                  <Pencil className="h-3.5 w-3.5" />
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 w-7 p-0 text-red-500 hover:bg-red-500/10 hover:text-red-600"
                  onClick={() => setDeletingIndex(idx)}
                  title={t('settings.modelRules.delete')}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 编辑弹窗 */}
      {editorOpen ? (
        <RuleEditDialog
          rule={editingIndex === null ? { match: '', headers: {}, body: {} } : value[editingIndex]}
          onSave={saveRule}
          onClose={closeEditor}
        />
      ) : null}

      {/* 删除确认 */}
      {deletingIndex !== null && (
        <Dialog open onOpenChange={() => setDeletingIndex(null)}>
          <DialogContent className="max-w-sm">
            <DialogHeader>
              <DialogTitle>{t('settings.modelRules.deleteConfirm')}</DialogTitle>
            </DialogHeader>
            <p className="text-sm text-muted-foreground">
              {tf('settings.modelRules.deleteHint', { match: value[deletingIndex].match })}
            </p>
            <DialogFooter>
              <Button size="sm" variant="outline" onClick={() => setDeletingIndex(null)}>
                {t('settings.upstream.cancel')}
              </Button>
              <Button size="sm" variant="destructive" onClick={() => deleteRule(deletingIndex)}>
                {t('settings.modelRules.delete')}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  )
}

// ========== 规则编辑弹窗 ==========
interface RuleEditDialogProps {
  rule: ModelRule
  onSave: (rule: ModelRule) => void
  onClose: () => void
}

function RuleEditDialog({ rule, onSave, onClose }: RuleEditDialogProps) {
  const { t, tf } = useI18n()
  const [match, setMatch] = useState(rule.match)
  type Entry = { k: string; v: string }
  const [headers, setHeaders] = useState<Entry[]>(() => Object.entries(rule.headers || {}).map(([k, v]) => ({ k, v })))
  const [body, setBody] = useState<Entry[]>(() =>
    Object.entries(rule.body || {}).map(([k, v]) => ({ k, v: typeof v === 'string' ? v : JSON.stringify(v) })),
  )
  const [newHeaderKey, setNewHeaderKey] = useState('')
  const [newHeaderVal, setNewHeaderVal] = useState('')
  const [newBodyKey, setNewBodyKey] = useState('')
  const [newBodyVal, setNewBodyVal] = useState('')

  const addHeader = () => {
    const k = newHeaderKey.trim()
    if (!k) return
    if (isCredentialHeader(k)) {
      toast(tf('settings.upstream.headerCredential', { k }), 'error')
      return
    }
    if (headers.some((entry) => entry.k === k)) {
      toast(t('settings.modelRules.jsonInvalid'), 'error')
      return
    }
    setHeaders([...headers, { k, v: newHeaderVal }])
    setNewHeaderKey('')
    setNewHeaderVal('')
  }

  const removeHeader = (index: number) => {
    setHeaders(headers.filter((_, i) => i !== index))
  }

  const addBody = () => {
    const k = newBodyKey.trim()
    if (!k) return
    if (body.some((entry) => entry.k === k)) {
      toast(t('settings.modelRules.jsonInvalid'), 'error')
      return
    }
    setBody([...body, { k, v: newBodyVal }])
    setNewBodyKey('')
    setNewBodyVal('')
  }

  const removeBody = (index: number) => {
    setBody(body.filter((_, i) => i !== index))
  }

  const insertPlaceholder = (field: 'header' | 'body', placeholder: string) => {
    if (field === 'header') {
      setNewHeaderVal((prev) => prev + placeholder)
    } else {
      setNewBodyVal((prev) => prev + placeholder)
    }
  }

  const handleSave = () => {
    if (!match.trim()) {
      toast(t('settings.modelRules.matchRequired'), 'error')
      return
    }

    // 校验凭证头
    const headerKeys = new Set<string>()
    for (const { k, v } of headers) {
      if (!k.trim() || headerKeys.has(k)) {
        toast(t('settings.modelRules.jsonInvalid'), 'error')
        return
      }
      headerKeys.add(k)
      if (isCredentialHeader(k) && v.trim()) {
        toast(tf('settings.upstream.headerCredential', { k }), 'error')
        return
      }
      if (PLACEHOLDER_HEADER_VAL.test(v)) {
        toast(tf('settings.upstream.headerPlaceholder', { k, v }), 'error')
        return
      }
    }

    // 解析 body（JSON 字符串转对象）
    const parsedBody: Record<string, unknown> = {}
    for (const { k, v } of body) {
      if (!k.trim() || Object.prototype.hasOwnProperty.call(parsedBody, k)) {
        toast(t('settings.modelRules.jsonInvalid'), 'error')
        return
      }
      try {
        // 尝试解析 JSON，失败则当字符串
        parsedBody[k] = JSON.parse(v)
      } catch {
        parsedBody[k] = v
      }
    }

    onSave({ match: match.trim(), headers: Object.fromEntries(headers.map(({ k, v }) => [k, v])), body: parsedBody })
  }

  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{rule.match ? t('settings.modelRules.editTitle') : t('settings.modelRules.addTitle')}</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          {/* 匹配模型 */}
          <div>
            <Label className="text-xs flex items-center gap-1.5">
              {t('settings.modelRules.matchLabel')}
              <TooltipProvider delayDuration={200}>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <AlertTriangle className="h-3 w-3 text-muted-foreground/60" />
                  </TooltipTrigger>
                  <TooltipContent className="max-w-xs text-xs">
                    {t('settings.modelRules.matchHint')}
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </Label>
            <Input
              className="mt-1 h-8 font-mono text-xs"
              value={match}
              onChange={(e) => setMatch(e.target.value)}
              placeholder="gpt-5.6-luna"
            />
          </div>

          {/* 注入请求头 */}
          <div>
            <div className="flex items-center justify-between">
              <Label className="text-xs">{t('settings.modelRules.headersLabel')}</Label>
              <div className="flex gap-1">
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 text-[10px]"
                  onClick={() => insertPlaceholder('header', '{{uuid}}')}
                >
                  {'{{uuid}}'}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 text-[10px]"
                  onClick={() => insertPlaceholder('header', '{{device_id}}')}
                >
                  {'{{device_id}}'}
                </Button>
              </div>
            </div>
            <div className="mt-1.5 space-y-1.5">
              {headers.map((entry, index) => (
                <div key={index} className="flex items-center gap-2">
                  <Input className="h-7 w-36 font-mono text-[11px]" value={entry.k} onChange={(e) => setHeaders(headers.map((item, i) => i === index ? { ...item, k: e.target.value } : item))} />
                  <Input className="h-7 flex-1 font-mono text-[11px]" value={entry.v} onChange={(e) => setHeaders(headers.map((item, i) => i === index ? { ...item, v: e.target.value } : item))} />
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 w-7 shrink-0 p-0 text-red-500"
                    onClick={() => removeHeader(index)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
              <div className="flex items-center gap-2">
                <Input
                  className="h-7 w-36 font-mono text-[11px]"
                  value={newHeaderKey}
                  onChange={(e) => setNewHeaderKey(e.target.value)}
                  placeholder={t('settings.modelRules.keyPlaceholder')}
                />
                <Input
                  className="h-7 flex-1 font-mono text-[11px]"
                  value={newHeaderVal}
                  onChange={(e) => setNewHeaderVal(e.target.value)}
                  placeholder={t('settings.modelRules.valuePlaceholder')}
                />
                <Button size="sm" variant="ghost" className="h-7 shrink-0 text-[11px]" onClick={addHeader}>
                  <Plus className="mr-1 h-3 w-3" />
                  {t('settings.modelRules.addHeader')}
                </Button>
              </div>
            </div>
            <p className="mt-1 text-[10px] text-muted-foreground">{t('settings.modelRules.headersHint')}</p>
          </div>

          {/* 注入 Body */}
          <div>
            <div className="flex items-center justify-between">
              <Label className="text-xs">{t('settings.modelRules.bodyLabel')}</Label>
              <div className="flex gap-1">
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 text-[10px]"
                  onClick={() => insertPlaceholder('body', '{{uuid}}')}
                >
                  {'{{uuid}}'}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 text-[10px]"
                  onClick={() => insertPlaceholder('body', '{{device_id}}')}
                >
                  {'{{device_id}}'}
                </Button>
              </div>
            </div>
            <div className="mt-1.5 space-y-1.5">
              {body.map((entry, index) => (
                <div key={index} className="flex items-start gap-2">
                  <Input className="h-7 w-36 font-mono text-[11px]" value={entry.k} onChange={(e) => setBody(body.map((item, i) => i === index ? { ...item, k: e.target.value } : item))} />
                  <Textarea
                    className="min-h-20 flex-1 font-mono text-[11px]"
                    value={entry.v}
                    onChange={(e) => setBody(body.map((item, i) => i === index ? { ...item, v: e.target.value } : item))}
                    placeholder={t('settings.modelRules.bodyValueHint')}
                  />
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 w-7 shrink-0 p-0 text-red-500"
                    onClick={() => removeBody(index)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
              <div className="flex items-start gap-2">
                <Input
                  className="h-7 w-36 font-mono text-[11px]"
                  value={newBodyKey}
                  onChange={(e) => setNewBodyKey(e.target.value)}
                  placeholder={t('settings.modelRules.keyPlaceholder')}
                />
                <Textarea
                  className="min-h-20 flex-1 font-mono text-[11px]"
                  value={newBodyVal}
                  onChange={(e) => setNewBodyVal(e.target.value)}
                  placeholder={t('settings.modelRules.bodyValueHint')}
                />
                <Button size="sm" variant="ghost" className="h-7 shrink-0 text-[11px]" onClick={addBody}>
                  <Plus className="mr-1 h-3 w-3" />
                  {t('settings.modelRules.addBody')}
                </Button>
              </div>
            </div>
            <p className="mt-1 text-[10px] text-muted-foreground">{t('settings.modelRules.bodyHint')}</p>
          </div>
        </div>

        <DialogFooter>
          <Button size="sm" variant="outline" onClick={onClose}>
            {t('settings.upstream.cancel')}
          </Button>
          <Button size="sm" onClick={handleSave}>
            {t('settings.upstream.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
