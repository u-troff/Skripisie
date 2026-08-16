import type { Language } from './types'

/** Stand-in for Piper on the Pi, so the loop runs end to end from a laptop. */
export function say(text: string, language: Language): void {
  if (typeof window.speechSynthesis === 'undefined') return
  const utterance = new SpeechSynthesisUtterance(text)
  utterance.lang = language === 'af' ? 'af-ZA' : 'en-ZA'
  window.speechSynthesis.cancel()
  window.speechSynthesis.speak(utterance)
}

export function stopSpeaking(): void {
  if (typeof window.speechSynthesis !== 'undefined') window.speechSynthesis.cancel()
}
