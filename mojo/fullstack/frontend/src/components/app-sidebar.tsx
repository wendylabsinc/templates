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
import {
  CameraIcon,
  AudioLinesIcon,
  HardDriveIcon,
  CpuIcon,
  InfoIcon,
  AlertCircleIcon,
} from "lucide-react"
import { useBackendHealth } from "@/hooks/use-backend-health"

const navItems = [
  { title: "Camera", to: "/camera", icon: CameraIcon },
  { title: "Audio", to: "/audio", icon: AudioLinesIcon },
  { title: "Persistence", to: "/persistence", icon: HardDriveIcon },
  { title: "GPU", to: "/gpu", icon: CpuIcon },
  { title: "System Information", to: "/system", icon: InfoIcon },
]

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  const backendHealthy = useBackendHealth()

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
                      <SidebarMenuButton
                        tooltip={item.title}
                        isActive={isActive}
                      >
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
      {!backendHealthy && (
        <SidebarFooter>
          <Alert variant="destructive" className="py-2">
            <AlertCircleIcon className="h-4 w-4" />
            <AlertDescription className="text-xs">
              Backend unreachable
            </AlertDescription>
          </Alert>
        </SidebarFooter>
      )}
    </Sidebar>
  )
}
