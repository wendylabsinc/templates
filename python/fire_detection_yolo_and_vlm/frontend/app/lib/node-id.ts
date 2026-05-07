const NODE_ID_KEY = "albert_nodeId"

export function getNodeId(): string {
  if (typeof window === "undefined") return "0"
  let nodeId = localStorage.getItem(NODE_ID_KEY)
  if (!nodeId) {
    // Generate a random UInt64 as a string
    const high = Math.floor(Math.random() * 0xFFFFFFFF)
    const low = Math.floor(Math.random() * 0xFFFFFFFF)
    nodeId = ((BigInt(high) << 32n) | BigInt(low)).toString()
    localStorage.setItem(NODE_ID_KEY, nodeId)
  }
  return nodeId
}

const USER_NAME_KEY = "albert_userName"

export function getUserName(): string {
  if (typeof window === "undefined") return ""
  return localStorage.getItem(USER_NAME_KEY) ?? ""
}

export function setUserName(name: string) {
  if (typeof window === "undefined") return
  localStorage.setItem(USER_NAME_KEY, name)
}
