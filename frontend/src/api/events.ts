import type { JobEvent } from '../types'

export function connectProjectEvents(projectId: string, after: number, onEvent: (event: JobEvent) => void, onState: (connected: boolean) => void) {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  let stopped = false
  let socket: WebSocket | null = null
  let timer = 0

  const connect = () => {
    if (stopped) return
    socket = new WebSocket(`${protocol}//${location.host}/api/v1/ws/projects/${projectId}?after=${after}`)
    socket.onopen = () => onState(true)
    socket.onmessage = (message) => {
      const event = JSON.parse(message.data) as JobEvent
      if (event.event_id) after = Math.max(after, event.event_id)
      onEvent(event)
    }
    socket.onclose = () => {
      onState(false)
      if (!stopped) timer = window.setTimeout(connect, 1500)
    }
  }
  connect()
  return () => {
    stopped = true
    window.clearTimeout(timer)
    socket?.close()
  }
}
