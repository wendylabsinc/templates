import { NavLink } from "react-router-dom"
import {
  Sidebar,
  SidebarContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarGroup,
  SidebarGroupContent,
} from "@/components/ui/sidebar"
import {
  CameraIcon,
  AudioLinesIcon,
  HardDriveIcon,
  CpuIcon,
  InfoIcon,
} from "lucide-react"

const navItems = [
  { title: "Camera", to: "/camera", icon: CameraIcon },
  { title: "Audio", to: "/audio", icon: AudioLinesIcon },
  { title: "Persistence", to: "/persistence", icon: HardDriveIcon },
  { title: "GPU", to: "/gpu", icon: CpuIcon },
  { title: "System Information", to: "/system", icon: InfoIcon },
]

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  return (
    <Sidebar collapsible="offcanvas" {...props}>
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton className="data-[slot=sidebar-menu-button]:p-1.5!">
              <img
                src="/assets/wendy-logo.svg"
                alt="Wendy"
                className="h-5 w-auto dark:invert"
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
    </Sidebar>
  )
}
