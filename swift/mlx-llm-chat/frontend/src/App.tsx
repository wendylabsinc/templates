import { useState, useRef, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Send, Bot, User, Loader2, Cpu, Zap } from "lucide-react";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  tokensGenerated?: number;
  generationTimeMs?: number;
}

interface ChatResponse {
  text: string;
  tokensGenerated: number;
  generationTimeMs: number;
}

function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const sendMessage = async () => {
    if (!input.trim() || isLoading) return;

    const userMessage: Message = {
      id: crypto.randomUUID(),
      role: "user",
      content: input.trim(),
    };

    setMessages((prev) => [...prev, userMessage]);
    setInput("");
    setIsLoading(true);

    try {
      const response = await fetch("/v1/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: userMessage.content, maxTokens: 512 }),
      });

      if (!response.ok) {
        throw new Error("Failed to get response");
      }

      const data: ChatResponse = await response.json();

      const assistantMessage: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: data.text,
        tokensGenerated: data.tokensGenerated,
        generationTimeMs: data.generationTimeMs,
      };

      setMessages((prev) => [...prev, assistantMessage]);
    } catch (error) {
      console.error("Error:", error);
      const errorMessage: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: "Sorry, there was an error processing your request.",
      };
      setMessages((prev) => [...prev, errorMessage]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  return (
    <div className="flex min-h-screen flex-col bg-background">
      {/* Header */}
      <header className="sticky top-0 z-10 border-b bg-card/80 backdrop-blur-sm">
        <div className="mx-auto flex max-w-4xl items-center gap-3 px-4 py-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary">
            <Cpu className="h-5 w-5 text-primary-foreground" />
          </div>
          <div>
            <h1 className="text-lg font-semibold">WendyOS MLX Chat</h1>
            <p className="text-xs text-muted-foreground">
              On-device LLM powered by MLX Swift
            </p>
          </div>
        </div>
      </header>

      {/* Chat Area */}
      <main className="flex-1 overflow-hidden">
        <ScrollArea className="h-[calc(100vh-8rem)]" ref={scrollRef}>
          <div className="mx-auto max-w-4xl px-4 py-6">
            {messages.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-20 text-center">
                <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-muted">
                  <Bot className="h-8 w-8 text-muted-foreground" />
                </div>
                <h2 className="text-xl font-semibold">Welcome to MLX Chat</h2>
                <p className="mt-2 max-w-md text-sm text-muted-foreground">
                  This is an on-device LLM running locally on your Jetson.
                  No data leaves your device. Start a conversation below.
                </p>
                <div className="mt-6 grid gap-2 sm:grid-cols-2">
                  <Card
                    className="cursor-pointer transition-colors hover:bg-accent"
                    onClick={() => setInput("What can you help me with?")}
                  >
                    <CardContent className="p-4 text-left text-sm">
                      What can you help me with?
                    </CardContent>
                  </Card>
                  <Card
                    className="cursor-pointer transition-colors hover:bg-accent"
                    onClick={() =>
                      setInput("Give me 3 things you can do on a Jetson Orin Nano.")
                    }
                  >
                    <CardContent className="p-4 text-left text-sm">
                      What can a Jetson Orin Nano do?
                    </CardContent>
                  </Card>
                </div>
              </div>
            ) : (
              <div className="space-y-6">
                {messages.map((message) => (
                  <div
                    key={message.id}
                    className={`flex gap-3 ${
                      message.role === "user" ? "flex-row-reverse" : ""
                    }`}
                  >
                    <Avatar className="h-8 w-8 shrink-0">
                      <AvatarFallback
                        className={
                          message.role === "user"
                            ? "bg-primary text-primary-foreground"
                            : "bg-muted"
                        }
                      >
                        {message.role === "user" ? (
                          <User className="h-4 w-4" />
                        ) : (
                          <Bot className="h-4 w-4" />
                        )}
                      </AvatarFallback>
                    </Avatar>
                    <div
                      className={`flex max-w-[80%] flex-col gap-1 ${
                        message.role === "user" ? "items-end" : "items-start"
                      }`}
                    >
                      <Card
                        className={`${
                          message.role === "user"
                            ? "bg-primary text-primary-foreground"
                            : "bg-muted"
                        }`}
                      >
                        <CardContent className="p-3">
                          <p className="whitespace-pre-wrap text-sm">
                            {message.content}
                          </p>
                        </CardContent>
                      </Card>
                      {message.role === "assistant" &&
                        message.tokensGenerated !== undefined && (
                          <div className="flex items-center gap-3 text-xs text-muted-foreground">
                            <span className="flex items-center gap-1">
                              <Zap className="h-3 w-3" />
                              {message.tokensGenerated} tokens
                            </span>
                            <span>
                              {message.generationTimeMs !== undefined &&
                                `${(message.generationTimeMs / 1000).toFixed(2)}s`}
                            </span>
                            {message.tokensGenerated !== undefined &&
                              message.generationTimeMs !== undefined &&
                              message.generationTimeMs > 0 && (
                                <span>
                                  {(
                                    (message.tokensGenerated /
                                      message.generationTimeMs) *
                                    1000
                                  ).toFixed(1)}{" "}
                                  tok/s
                                </span>
                              )}
                          </div>
                        )}
                    </div>
                  </div>
                ))}
                {isLoading && (
                  <div className="flex gap-3">
                    <Avatar className="h-8 w-8 shrink-0">
                      <AvatarFallback className="bg-muted">
                        <Bot className="h-4 w-4" />
                      </AvatarFallback>
                    </Avatar>
                    <Card className="bg-muted">
                      <CardContent className="flex items-center gap-2 p-3">
                        <Loader2 className="h-4 w-4 animate-spin" />
                        <span className="text-sm text-muted-foreground">
                          Generating...
                        </span>
                      </CardContent>
                    </Card>
                  </div>
                )}
              </div>
            )}
          </div>
        </ScrollArea>
      </main>

      {/* Input Area */}
      <footer className="sticky bottom-0 border-t bg-card/80 backdrop-blur-sm">
        <div className="mx-auto max-w-4xl px-4 py-3">
          <div className="flex gap-2">
            <Input
              placeholder="Type a message..."
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={isLoading}
              className="flex-1 bg-background"
            />
            <Button
              onClick={sendMessage}
              disabled={!input.trim() || isLoading}
              size="icon"
            >
              {isLoading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Send className="h-4 w-4" />
              )}
            </Button>
          </div>
          <p className="mt-2 text-center text-xs text-muted-foreground">
            Running locally on Jetson via MLX Swift
          </p>
        </div>
      </footer>
    </div>
  );
}

export default App;
