import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProjectsStore } from './projects'

describe('projects store events', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('keeps websocket event ids ordered and deduplicated', () => {
    const store = useProjectsStore()
    const event = { event_id: 7, job_id: 'job', type: 'chapter.reviewed', timestamp: '2026-01-01T00:00:00Z', payload: {} }
    store.receiveEvent(event)
    store.receiveEvent(event)
    expect(store.events).toHaveLength(1)
    expect(store.latestEventId).toBe(7)
  })

  it('keeps only the latest 50 websocket events', () => {
    const store = useProjectsStore()
    for (let eventId = 1; eventId <= 60; eventId += 1) {
      store.receiveEvent({ event_id: eventId, job_id: 'job', type: 'log', timestamp: '2026-01-01T00:00:00Z', payload: {} })
    }
    expect(store.events).toHaveLength(50)
    expect(store.events[0].event_id).toBe(11)
    expect(store.latestEventId).toBe(60)
  })
})
