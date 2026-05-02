// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { Joyride, type EventData, STATUS, type Step } from 'react-joyride';
import { useNavigate } from 'react-router-dom';
import { useTourState } from './useTourState';

const STEPS: Step[] = [
  {
    target: 'body',
    content: 'Your DevOps intelligence platform is ready. Let me show you around -- this takes about 30 seconds.',
    placement: 'center',
    skipBeacon: true,
    title: 'Welcome to MEHO!',
  },
  {
    target: '[href="/chat"]',
    placement: 'right',
    title: 'Investigate with Chat',
    content: 'Ask questions about your infrastructure in natural language. MEHO connects the dots across all your systems.',
  },
  {
    target: '[href="/connectors"]',
    placement: 'right',
    title: 'Connect Your Systems',
    content: 'Link Kubernetes, Prometheus, GitLab, and more. Connectors give MEHO access to real-time data from your stack.',
  },
  {
    target: '[href="/knowledge"]',
    placement: 'right',
    title: 'Add Your Documentation',
    content: 'Upload runbooks, architecture docs, and troubleshooting guides. MEHO references these during investigations.',
  },
  {
    target: '[href="/topology"]',
    placement: 'right',
    title: 'See How Systems Connect',
    content: 'Topology maps your infrastructure relationships. MEHO uses this graph for cross-system root cause analysis.',
  },
  {
    target: '[href="/connectors"]',
    placement: 'right',
    title: 'Add Your First Connector',
    content: 'Ready to start? Add a connector and then ask MEHO your first question. Kubernetes and REST APIs are great starting points.',
  },
];

const joyrideStyles = {
  tooltip: {
    borderRadius: '12px',
    padding: '24px',
    border: '1px solid rgba(128, 81, 184, 0.3)',
    boxShadow: '0 0 20px rgba(128, 81, 184, 0.1), 0 10px 15px -3px rgba(0, 0, 0, 0.5)',
    fontFamily: "'Poppins', system-ui, -apple-system, sans-serif",
  },
  tooltipTitle: {
    fontSize: '16px',
    fontWeight: 600,
    lineHeight: 1.3,
    color: '#FAFAFA',
    marginBottom: '8px',
  },
  tooltipContent: {
    fontSize: '14px',
    fontWeight: 400,
    lineHeight: 1.5,
    color: '#A1A1AA',
    padding: 0,
  },
  buttonPrimary: {
    backgroundColor: '#8051B8',
    borderRadius: '8px',
    fontSize: '14px',
    fontWeight: 400,
    padding: '8px 16px',
    outline: 'none',
  },
  buttonBack: {
    color: '#A1A1AA',
    fontSize: '14px',
    fontWeight: 400,
    marginRight: '8px',
  },
  buttonSkip: {
    color: '#71717A',
    fontSize: '12px',
    fontWeight: 400,
    outline: 'none',
  },
};

export function FirstRunTour() {
  const navigate = useNavigate();
  const { shouldShowTour, completeTour } = useTourState();

  const handleEvent = (data: EventData) => {
    const { status } = data;

    if (status === STATUS.FINISHED) {
      completeTour();
      navigate('/connectors');
    } else if (status === STATUS.SKIPPED) {
      completeTour();
    }
  };

  if (!shouldShowTour) return null;

  return (
    <Joyride
      steps={STEPS}
      run={shouldShowTour}
      continuous
      onEvent={handleEvent}
      options={{
        arrowColor: '#18181B',
        backgroundColor: '#18181B',
        overlayColor: 'rgba(15, 15, 18, 0.75)',
        primaryColor: '#8051B8',
        textColor: '#FAFAFA',
        zIndex: 10000,
        overlayClickAction: false,
        buttons: ['back', 'close', 'primary', 'skip'],
        spotlightRadius: 12,
      }}
      styles={joyrideStyles}
      locale={{
        back: 'Back',
        close: 'Close',
        last: "Let's go!",
        next: 'Next',
        open: 'Open the dialog',
        skip: 'Skip tour',
      }}
    />
  );
}
