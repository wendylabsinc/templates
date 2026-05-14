import * as React from "react"
import { PromptBox } from "@/components/ui/chatgpt-prompt-input"

type ChatInputProps = {
  onSend: (value: string) => void
  autoFocus?: boolean
}

export function ChatInput({ onSend, autoFocus }: ChatInputProps) {
  const ref = React.useRef<HTMLTextAreaElement>(null)
  const [resetKey, setResetKey] = React.useState(0)

  const submit = () => {
    const v = ref.current?.value.trim() ?? ""
    if (!v) return
    onSend(v)
    setResetKey((k) => k + 1)
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        submit()
      }}
    >
      <PromptBox
        key={resetKey}
        ref={ref}
        autoFocus={autoFocus}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault()
            submit()
          }
        }}
      />
    </form>
  )
}
