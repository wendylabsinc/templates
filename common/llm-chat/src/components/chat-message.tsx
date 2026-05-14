import * as React from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { Check, Copy } from "lucide-react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { ChatMessage } from "@/lib/threads"

function CopyButton({ text, className }: { text: string; className?: string }) {
  const [copied, setCopied] = React.useState(false)

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // ignore
    }
  }

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      aria-label={copied ? "Copied" : "Copy message"}
      onClick={handleCopy}
      className={cn("size-7 text-muted-foreground", className)}
    >
      {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
    </Button>
  )
}

const markdownComponents = {
  p: (props: React.ComponentProps<"p">) => (
    <p {...props} className="m-0 whitespace-pre-wrap break-words" />
  ),
}

export function ChatMessageView({ message }: { message: ChatMessage }) {
  if (message.role === "user") {
    return (
      <div className="group flex justify-end py-3">
        <div className="flex max-w-[75%] items-start gap-1">
          <CopyButton
            text={message.content}
            className="mt-1 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
          />
          <div className="rounded-3xl bg-secondary px-4 py-2.5 text-secondary-foreground break-words">
            <div className="prose prose-sm prose-neutral dark:prose-invert max-w-none prose-p:m-0">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={markdownComponents}
              >
                {message.content}
              </ReactMarkdown>
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="py-3">
      <div
        className={cn(
          "prose prose-neutral dark:prose-invert max-w-none",
          "prose-pre:bg-muted prose-pre:text-foreground",
          "prose-code:before:content-none prose-code:after:content-none",
        )}
      >
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {message.content || "…"}
        </ReactMarkdown>
      </div>
    </div>
  )
}
