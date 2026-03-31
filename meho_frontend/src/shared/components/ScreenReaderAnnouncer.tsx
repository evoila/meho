// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Screen Reader Announcer (Phase 64-02)
 *
 * Globally-mounted component that reads the Zustand announcement queue and
 * renders aria-live regions for screen reader users. Polite announcements
 * queue behind speech; assertive announcements interrupt immediately.
 *
 * Announcements auto-clear after 5 seconds to prevent stale content.
 */
import { useEffect, useRef } from 'react';
import { useChatStore } from '@/features/chat/stores';

export function ScreenReaderAnnouncer() {
  const announcements = useChatStore((s) => s.announcements);
  const clearAnnouncement = useChatStore((s) => s.clearAnnouncement);
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  useEffect(() => {
    // Set up auto-clear timers for new announcements
    for (const ann of announcements) {
      if (!timersRef.current.has(ann.id)) {
        const timer = setTimeout(() => {
          clearAnnouncement(ann.id);
          timersRef.current.delete(ann.id);
        }, 5000);
        timersRef.current.set(ann.id, timer);
      }
    }

    // Cleanup timers for announcements that were removed externally
    for (const [id, timer] of timersRef.current) {
      if (!announcements.find((a) => a.id === id)) {
        clearTimeout(timer);
        timersRef.current.delete(id);
      }
    }
  }, [announcements, clearAnnouncement]);

  // Cleanup all timers on unmount
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      for (const timer of timers.values()) {
        clearTimeout(timer);
      }
      timers.clear();
    };
  }, []);

  const polite = announcements.filter((a) => a.priority === 'polite');
  const assertive = announcements.filter((a) => a.priority === 'assertive');

  return (
    <>
      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {polite.map((ann) => (
          <span key={ann.id}>{ann.message}</span>
        ))}
      </div>
      <div className="sr-only" aria-live="assertive" aria-atomic="true">
        {assertive.map((ann) => (
          <span key={ann.id}>{ann.message}</span>
        ))}
      </div>
    </>
  );
}
