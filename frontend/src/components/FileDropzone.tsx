import { ClipboardPaste, FolderOpen, ImagePlus } from 'lucide-react'
import { useCallback, useEffect, useId, useRef, useState } from 'react'

interface FileDropzoneProps {
  onFiles: (files: File[]) => void
  disabled?: boolean
  multiple?: boolean
  maxFiles?: number
  label?: string
  detail?: string
}

export function FileDropzone({ onFiles, disabled = false, multiple = true, maxFiles, label = '添加图片', detail }: FileDropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const inputId = useId()
  const [dragging, setDragging] = useState(false)

  const acceptFiles = useCallback((files: FileList | File[]) => {
    const selected = Array.from(files)
      .filter((file) => file.type.startsWith('image/'))
      .slice(0, maxFiles ?? (multiple ? undefined : 1))
    if (selected.length) onFiles(selected)
  }, [maxFiles, multiple, onFiles])

  useEffect(() => {
    const onPaste = (event: ClipboardEvent) => {
      if (disabled) return
      const target = event.target instanceof HTMLElement ? event.target : null
      if (target?.closest('input, textarea, select, [contenteditable="true"]')) return
      const files = event.clipboardData?.files
      if (files?.length) acceptFiles(files)
    }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [acceptFiles, disabled])

  return (
    <div
      className={`dropzone ${dragging ? 'dropzone-active' : ''} ${disabled ? 'dropzone-disabled' : ''}`}
      onDragEnter={(event) => { event.preventDefault(); if (!disabled) setDragging(true) }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={(event) => { if (event.currentTarget === event.target) setDragging(false) }}
      onDrop={(event) => { event.preventDefault(); setDragging(false); if (!disabled) acceptFiles(event.dataTransfer.files) }}
      onClick={() => { if (!disabled) inputRef.current?.click() }}
      onKeyDown={(event) => {
        if ((event.key === 'Enter' || event.key === ' ') && !disabled) {
          event.preventDefault()
          inputRef.current?.click()
        }
      }}
      tabIndex={disabled ? -1 : 0}
      role="button"
      aria-label={label}
      aria-disabled={disabled}
      aria-controls={inputId}
    >
      <input id={inputId} ref={inputRef} type="file" accept="image/*" multiple={multiple} hidden disabled={disabled} onChange={(event) => { if (event.target.files) acceptFiles(event.target.files); event.target.value = '' }} />
      <div className="dropzone-icon"><ImagePlus size={25} aria-hidden="true" /></div>
      <strong>拖入图片到这里</strong>
      <span>{detail ?? '或选择文件，也可以直接粘贴截图'}</span>
      <div className="dropzone-actions">
        <span className="button button-secondary button-sm" aria-hidden="true"><FolderOpen size={15} aria-hidden="true" /><span>选择图片</span></span>
        <span className="dropzone-paste"><ClipboardPaste size={14} aria-hidden="true" /> Ctrl + V</span>
      </div>
    </div>
  )
}
