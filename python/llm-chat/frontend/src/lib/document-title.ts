import * as React from "react"

const DEFAULT_TITLE = "Wendy LLM Chat"

export function useDocumentTitle(title?: string) {
  React.useEffect(() => {
    const next = title?.trim() ? `${title} — ${DEFAULT_TITLE}` : DEFAULT_TITLE
    const prev = document.title
    document.title = next
    return () => {
      document.title = prev
    }
  }, [title])
}
