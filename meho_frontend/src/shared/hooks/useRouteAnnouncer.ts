// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Route Announcer Hook (Phase 64: Accessibility)
 *
 * Manages focus on client-side route changes so screen reader users
 * immediately hear the new page context. On path change, finds the
 * page's <h1>, temporarily makes it focusable, and calls .focus().
 */
import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';

export function useRouteAnnouncer(): void {
  const { pathname } = useLocation();
  const previousPathRef = useRef(pathname);

  useEffect(() => {
    if (pathname === previousPathRef.current) return;
    previousPathRef.current = pathname;

    // Use requestAnimationFrame to ensure the new page has rendered its <h1>
    requestAnimationFrame(() => {
      const h1 = document.querySelector('h1');
      if (!h1) return;

      // Make it temporarily focusable (h1 is not focusable by default)
      h1.setAttribute('tabindex', '-1');
      h1.focus();

      // Remove tabindex on blur so it doesn't interfere with normal tab order
      h1.addEventListener(
        'blur',
        () => {
          h1.removeAttribute('tabindex');
        },
        { once: true },
      );
    });
  }, [pathname]);
}
