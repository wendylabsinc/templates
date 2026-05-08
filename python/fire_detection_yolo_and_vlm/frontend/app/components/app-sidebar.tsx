"use client"

import * as React from "react"

import { NavCameras } from "~/components/nav-cameras"
import { NavConversations } from "~/components/nav-conversations"
import { NavSecondary } from "~/components/nav-secondary"
import { NavUser } from "~/components/nav-user"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "~/components/ui/sidebar"
import { Settings2Icon, SearchIcon, ScanEyeIcon } from "lucide-react"

const data = {
  navSecondary: [
    {
      title: "Detection",
      url: "/",
      icon: <ScanEyeIcon />,
    },
    {
      title: "Settings",
      url: "#",
      icon: <Settings2Icon />,
    },
    {
      title: "Search",
      url: "#",
      icon: <SearchIcon />,
    },
  ],
}

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  return (
    <Sidebar collapsible="offcanvas" {...props}>
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              className="data-[slot=sidebar-menu-button]:p-1.5!"
              render={<a href="/" />}
            >
              <div className="flex size-6! items-center justify-center rounded bg-black p-1">
                <img src="/albert_logo.svg" alt="Albert" className="size-full" />
              </div>
              <span className="text-base font-semibold">Albert</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        <NavConversations />
        <NavCameras />
        <NavSecondary items={data.navSecondary} className="mt-auto" />
      </SidebarContent>
      <SidebarFooter>
        <div className="flex items-start py-2">
          <img
            src="/wendy_logo.svg"
            alt="Powered by Wendy"
            className="h-6 opacity-40"
            style={{ filter: "var(--wendy-logo-filter, none)" }}
          />
        </div>
        <NavUser />
      </SidebarFooter>
    </Sidebar>
  )
}
