import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { PromptDefaults } from '../types'

export const FALLBACK_ONLINE_PROMPTS: PromptDefaults = {
  nl_prompt: 'Instruction (Deep Scan):\nRigorously analyze the image for anatomy, pose, attire, texture, lighting, background, and all important visible details.\n\nTask:\nSynthesize a dense, highly descriptive caption in English. Do not simplify or summarize.',
  tag_prompt: 'Generate a comprehensive list of booru-style tags for this image. Include anatomy, body features, clothing states, actions, background, and artistic style. Be explicit and precise. Tags should be separated by commas, in English.',
  json_prompt: 'Analyze this image and return one strict JSON object for Anima training captions. Return JSON only with exactly the nine fields quality, count, character, series, artist, appearance, tags, environment, nl. Use English booru-style tags and write a detailed natural-language caption in nl.',
}

const STORAGE_KEY = 'tagger2-online-prompts'

function loadStored(): PromptDefaults | null {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null') as Partial<PromptDefaults> | null
    if (value?.tag_prompt && value.nl_prompt && value.json_prompt) return { ...FALLBACK_ONLINE_PROMPTS, ...value }
  } catch {
    // Ignore malformed local preferences and use defaults.
  }
  return null
}

export function useOnlinePrompts() {
  const query = useQuery({ queryKey: ['prompt-defaults'], queryFn: api.promptDefaults, staleTime: Infinity, retry: false })
  const stored = useRef(loadStored())
  const [prompts, setPrompts] = useState<PromptDefaults>(stored.current ?? FALLBACK_ONLINE_PROMPTS)
  const initializedFromServer = useRef(Boolean(stored.current))

  useEffect(() => {
    if (!initializedFromServer.current && query.data?.tag_prompt && query.data.nl_prompt && query.data.json_prompt) {
      setPrompts(query.data)
      initializedFromServer.current = true
    }
  }, [query.data])

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(prompts))
    } catch {
      // Storage can be unavailable in hardened browser contexts.
    }
  }, [prompts])

  const reset = () => {
    setPrompts(query.data ?? FALLBACK_ONLINE_PROMPTS)
    initializedFromServer.current = true
  }
  return { ...prompts, setPrompts, reset, loading: query.isLoading }
}
