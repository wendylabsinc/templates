import { NavLink } from "react-router-dom"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarGroup,
  SidebarGroupContent,
} from "@/components/ui/sidebar"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { ActivityIcon, RadioIcon, WavesIcon, AlertCircleIcon } from "lucide-react"
import { useSensing } from "@/hooks/use-sensing-stream"

const navItems = [
  { title: "Live", to: "/live", icon: ActivityIcon },
  { title: "Sensors", to: "/sensors", icon: RadioIcon },
  { title: "CSI Waterfall", to: "/waterfall", icon: WavesIcon },
]

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  const { status } = useSensing()

  return (
    <Sidebar collapsible="offcanvas" {...props}>
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton className="data-[slot=sidebar-menu-button]:p-1.5!">
              <img
                src="/assets/wendy-logo.svg"
                alt="Wendy"
                className="h-5 w-auto invert dark:invert-0"
              />
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              {navItems.map((item) => (
                <SidebarMenuItem key={item.title}>
                  <NavLink to={item.to}>
                    {({ isActive }) => (
                      <SidebarMenuButton tooltip={item.title} isActive={isActive}>
                        <item.icon />
                        <span>{item.title}</span>
                      </SidebarMenuButton>
                    )}
                  </NavLink>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      {status !== "open" && (
        <SidebarFooter>
          <Alert variant="destructive" className="py-2">
            <AlertCircleIcon className="h-4 w-4" />
            <AlertDescription className="text-xs">
              {status === "connecting" ? "Connecting to stream…" : "Stream disconnected"}
            </AlertDescription>
          </Alert>
        </SidebarFooter>
      )}
    </Sidebar>
  )
}
