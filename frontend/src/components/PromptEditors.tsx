import { RotateCcw } from 'lucide-react'
import type { PromptDefaults } from '../types'
import { Button, Field } from './ui'

export function PromptEditors({ prompts, onChange, onReset, disabled = false, fields = ['tag_prompt', 'nl_prompt', 'json_prompt'] }: {
  prompts: PromptDefaults
  onChange: (next: PromptDefaults) => void
  onReset: () => void
  disabled?: boolean
  fields?: Array<keyof PromptDefaults>
}) {
  const labels: Record<keyof PromptDefaults, string> = {
    tag_prompt: 'TAG Prompt',
    nl_prompt: 'NL Prompt',
    json_prompt: 'JSON Prompt',
  }
  return <div className="prompt-editor-block">
    <div className="prompt-editor-heading"><span>在线提示词</span><Button type="button" size="sm" variant="quiet" icon={<RotateCcw size={13} />} onClick={onReset}>恢复默认</Button></div>
    <div className={`prompt-editor-grid ${fields.length === 1 ? 'prompt-editor-grid-single' : ''}`}>
      {fields.map((field) => <Field label={labels[field]} key={field}><textarea disabled={disabled} value={prompts[field]} onChange={(event) => onChange({ ...prompts, [field]: event.target.value })} /></Field>)}
    </div>
  </div>
}
