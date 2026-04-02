import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"

export default function PersistencePage() {
  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Persistence</h1>
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm font-medium">Persistent Volume</CardTitle>
              <Badge variant="outline">persist</Badge>
            </div>
            <CardDescription>
              Data stored here survives container restarts and redeployments.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-3 text-sm">
              <div className="flex justify-between">
                <span className="text-muted-foreground">Mount Path</span>
                <code className="rounded bg-muted px-1.5 py-0.5">/data</code>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Volume Name</span>
                <code className="rounded bg-muted px-1.5 py-0.5">app-data</code>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Host Path</span>
                <code className="rounded bg-muted px-1.5 py-0.5">/var/lib/wendy/volumes/app-data</code>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">How to Use</CardTitle>
            <CardDescription>
              Add the persist entitlement to your wendy.json
            </CardDescription>
          </CardHeader>
          <CardContent>
            <pre className="overflow-x-auto rounded-lg bg-muted p-4 text-xs">
{`{
  "entitlements": [
    {
      "type": "persist",
      "name": "app-data",
      "path": "/data"
    }
  ]
}`}
            </pre>
          </CardContent>
        </Card>
      </div>
      <p className="text-sm text-muted-foreground">
        Files written to the mount path are persisted on the host at{" "}
        <code className="rounded bg-muted px-1 py-0.5">/var/lib/wendy/volumes/&lt;name&gt;</code>.
        Multiple apps can share data by using the same volume name.
      </p>
    </div>
  )
}
