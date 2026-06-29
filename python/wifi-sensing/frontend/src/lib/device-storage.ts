export interface Device {
  id: string
  name: string
}

export function getStoredDevice(storageKey: string): string {
  if (typeof window === "undefined") return ""

  try {
    return window.localStorage.getItem(storageKey) ?? ""
  } catch {
    return ""
  }
}

export function storeDevice(storageKey: string, deviceId: string) {
  if (typeof window === "undefined" || !deviceId) return

  try {
    window.localStorage.setItem(storageKey, deviceId)
  } catch {
    // Ignore storage failures so device switching still works in restricted browsers.
  }
}

export function resolveDeviceSelection(devices: Device[], currentDevice: string, storageKey: string): string {
  if (devices.length === 0) return currentDevice
  if (currentDevice && devices.some((device) => device.id === currentDevice)) return currentDevice

  const storedDevice = getStoredDevice(storageKey)
  if (storedDevice && devices.some((device) => device.id === storedDevice)) return storedDevice

  return devices[0].id
}
