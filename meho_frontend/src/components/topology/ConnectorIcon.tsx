// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ConnectorIcon - SVG icon component for connector types (Phase 17)
 *
 * Renders distinct geometric SVG icons for each supported connector type.
 * Icons are simple shapes optimized for small sizes (16-24px).
 */

import React from 'react';

// ============================================================================
// Color Map (exported for reuse in other components)
// ============================================================================

export const CONNECTOR_COLORS: Record<string, string> = {
  kubernetes: '#3B82F6',  // blue
  vmware: '#10B981',      // emerald
  gcp: '#EF4444',         // red
  proxmox: '#F97316',     // orange
  rest: '#6B7280',        // gray (default/fallback)
  soap: '#F59E0B',        // amber
  graphql: '#EC4899',     // pink
  grpc: '#6366F1',        // indigo
  prometheus: '#E6522C',  // Prometheus brand orange-red
  loki: '#F9A825',        // Warm amber/yellow for logs
  tempo: '#00BCD4',       // Cyan/teal for traces
  alertmanager: '#E53E3E', // Cool red for alerts
  jira: '#0052CC',         // Jira brand blue
  confluence: '#1868DB',    // Confluence brand blue
  email: '#22C55E',         // Green -- universally "sent/delivered"
  azure: '#0078D4',         // Azure brand blue
  aws: '#FF9900',            // AWS orange
  slack: '#611F69',          // Slack brand aubergine purple
};

// ============================================================================
// Props
// ============================================================================

interface ConnectorIconProps {
  connectorType: string;
  size?: number;
  className?: string;
}

// ============================================================================
// Per-type SVG paths
// ============================================================================

/**
 * Kubernetes: Simplified helm wheel -- circle with 7 spokes radiating from center.
 */
function KubernetesIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  // 7 spokes at equal angles (360/7 ~ 51.43 degrees)
  const spokes = Array.from({ length: 7 }, (_, i) => {
    const angle = (i * 360) / 7 - 90; // Start from top
    const rad = (angle * Math.PI) / 180;
    const innerR = 4;
    const outerR = 9;
    return (
      <line
        key={i}
        x1={12 + innerR * Math.cos(rad)}
        y1={12 + innerR * Math.sin(rad)}
        x2={12 + outerR * Math.cos(rad)}
        y2={12 + outerR * Math.sin(rad)}
        stroke={color}
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    );
  });

  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="12" cy="12" r="10" stroke={color} strokeWidth="1.5" fill="none" />
      <circle cx="12" cy="12" r="3" fill={color} />
      {spokes}
    </svg>
  );
}

/**
 * VMware: Diamond shape (rotated square) -- represents VMware's diamond logo.
 */
function VmwareIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path
        d="M12 2 L22 12 L12 22 L2 12 Z"
        fill={color}
        fillOpacity="0.15"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path
        d="M12 7 L17 12 L12 17 L7 12 Z"
        fill={color}
        fillOpacity="0.4"
        stroke={color}
        strokeWidth="1"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * GCP: Hexagon shape -- represents GCP's cloud hexagon.
 */
function GcpIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  // Regular hexagon centered at (12, 12) with radius 10
  const points = Array.from({ length: 6 }, (_, i) => {
    const angle = (i * 60 - 30) * (Math.PI / 180);
    return `${12 + 10 * Math.cos(angle)},${12 + 10 * Math.sin(angle)}`;
  }).join(' ');

  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <polygon
        points={points}
        fill={color}
        fillOpacity="0.15"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <circle cx="12" cy="12" r="3.5" fill={color} fillOpacity="0.5" />
    </svg>
  );
}

/**
 * Proxmox: Cube/box shape -- represents server/hypervisor with 3 visible faces.
 */
function ProxmoxIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      {/* Top face */}
      <path
        d="M12 3 L21 8 L12 13 L3 8 Z"
        fill={color}
        fillOpacity="0.3"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      {/* Left face */}
      <path
        d="M3 8 L12 13 L12 22 L3 17 Z"
        fill={color}
        fillOpacity="0.15"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      {/* Right face */}
      <path
        d="M21 8 L12 13 L12 22 L21 17 Z"
        fill={color}
        fillOpacity="0.08"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Prometheus: Flame/fire shape -- represents Prometheus's fire-bringing theme.
 * Simplified: upward triangle with inner circle, evoking a stylized flame.
 */
function PrometheusIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      {/* Outer flame/triangle shape */}
      <path
        d="M12 2 L21 20 L3 20 Z"
        fill={color}
        fillOpacity="0.15"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      {/* Inner circle -- the Prometheus fire */}
      <circle cx="12" cy="14" r="4" fill={color} fillOpacity="0.4" stroke={color} strokeWidth="1" />
      {/* Center dot */}
      <circle cx="12" cy="14" r="1.5" fill={color} />
    </svg>
  );
}

/**
 * Loki: Log/document shape -- a rectangle with horizontal lines representing log entries.
 * Distinct from Prometheus flame shape, evokes a log file or scroll.
 */
function LokiIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      {/* Document/scroll shape */}
      <rect
        x="4" y="2" width="16" height="20" rx="2"
        fill={color}
        fillOpacity="0.15"
        stroke={color}
        strokeWidth="1.5"
      />
      {/* Log lines */}
      <line x1="7" y1="7" x2="17" y2="7" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      <line x1="7" y1="11" x2="15" y2="11" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      <line x1="7" y1="15" x2="13" y2="15" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Tempo: Trace waterfall shape -- three horizontal lines of decreasing length
 * with circles at line starts representing span start points.
 * Visually suggests a distributed trace waterfall / flame chart.
 */
function TempoIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      {/* Background rounded rect */}
      <rect
        x="3" y="3" width="18" height="18" rx="3"
        fill={color}
        fillOpacity="0.1"
        stroke={color}
        strokeWidth="1.2"
      />
      {/* Span 1: longest bar (root span) */}
      <circle cx="6" cy="8" r="1.3" fill={color} />
      <line x1="7.5" y1="8" x2="19" y2="8" stroke={color} strokeWidth="2" strokeLinecap="round" />
      {/* Span 2: medium bar (child span, indented) */}
      <circle cx="8.5" cy="12" r="1.3" fill={color} fillOpacity="0.7" />
      <line x1="10" y1="12" x2="17" y2="12" stroke={color} strokeWidth="2" strokeLinecap="round" opacity="0.7" />
      {/* Span 3: short bar (grandchild span, more indented) */}
      <circle cx="11" cy="16" r="1.3" fill={color} fillOpacity="0.45" />
      <line x1="12.5" y1="16" x2="15" y2="16" stroke={color} strokeWidth="2" strokeLinecap="round" opacity="0.45" />
    </svg>
  );
}

/**
 * Alertmanager: Bell/alert shape -- represents notifications and alert management.
 * A bell silhouette with a small exclamation mark, evoking alert notifications.
 */
function AlertmanagerIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      {/* Bell body */}
      <path
        d="M12 3C8.13 3 5 6.13 5 10V15L3 17V18H21V17L19 15V10C19 6.13 15.87 3 12 3Z"
        fill={color}
        fillOpacity="0.15"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      {/* Bell clapper / bottom */}
      <path
        d="M9.5 19C9.5 20.38 10.62 21.5 12 21.5C13.38 21.5 14.5 20.38 14.5 19"
        stroke={color}
        strokeWidth="1.5"
        strokeLinecap="round"
      />
      {/* Exclamation dot */}
      <circle cx="12" cy="14" r="1.2" fill={color} />
      {/* Exclamation line */}
      <line x1="12" y1="7" x2="12" y2="11.5" stroke={color} strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Jira: Stylized angular diamond -- represents the Jira logo shape.
 * Two overlapping angular shapes forming the distinctive Jira mark.
 */
function JiraIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      {/* Upper-right angular shape */}
      <path
        d="M12 2 L22 12 L12 12 Z"
        fill={color}
        fillOpacity="0.4"
        stroke={color}
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      {/* Lower-left angular shape */}
      <path
        d="M12 12 L12 22 L2 12 Z"
        fill={color}
        fillOpacity="0.4"
        stroke={color}
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      {/* Center connecting diamond */}
      <path
        d="M8 12 L12 8 L16 12 L12 16 Z"
        fill={color}
        fillOpacity="0.7"
        stroke={color}
        strokeWidth="1"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Confluence: Overlapping page shapes -- represents the Confluence wiki/document logo.
 * Two overlapping rounded rectangles suggesting stacked pages of documentation.
 */
function ConfluenceIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      {/* Back page */}
      <rect
        x="6" y="3" width="14" height="16" rx="2"
        fill={color}
        fillOpacity="0.15"
        stroke={color}
        strokeWidth="1.2"
      />
      {/* Front page */}
      <rect
        x="4" y="5" width="14" height="16" rx="2"
        fill={color}
        fillOpacity="0.3"
        stroke={color}
        strokeWidth="1.5"
      />
      {/* Content lines on front page */}
      <line x1="7" y1="10" x2="15" y2="10" stroke={color} strokeWidth="1.3" strokeLinecap="round" />
      <line x1="7" y1="13.5" x2="13" y2="13.5" stroke={color} strokeWidth="1.3" strokeLinecap="round" />
      <line x1="7" y1="17" x2="11" y2="17" stroke={color} strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Email: Envelope shape -- rectangle with triangular flap representing an email/mail icon.
 * Uses green (#22C55E) for "sent/delivered" association.
 */
function EmailIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      {/* Envelope body */}
      <rect
        x="3" y="5" width="18" height="14" rx="2"
        fill={color}
        fillOpacity="0.15"
        stroke={color}
        strokeWidth="1.5"
      />
      {/* Envelope flap / V shape */}
      <path
        d="M3 7 L12 13 L21 7"
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Azure: Stylized "A" shape reminiscent of the Azure logo -- a diamond/parallelogram
 * with a horizontal bar, evoking the Azure brand mark.
 */
function AzureIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      {/* Outer diamond shape */}
      <path
        d="M12 2 L21 12 L12 22 L3 12 Z"
        fill={color}
        fillOpacity="0.15"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      {/* Inner A-like shape with horizontal bar */}
      <path
        d="M12 6 L17 12 L15 12 L12 8 L9 12 L7 12 Z"
        fill={color}
        fillOpacity="0.5"
      />
      {/* Horizontal bar */}
      <line x1="8" y1="14" x2="16" y2="14" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      {/* Lower legs of A */}
      <path
        d="M9 12 L7 18 M15 12 L17 18"
        fill="none"
        stroke={color}
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}

/**
 * AWS: Upward-pointing arrow/chevron -- evokes the AWS logo's smile/arrow motif.
 */
function AwsIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path
        d="M4 16 L12 8 L20 16"
        stroke={color}
        strokeWidth="3"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
      <path
        d="M8 20 L12 16 L16 20"
        stroke={color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );
}

/**
 * Slack: Simplified hash/pound symbol representing Slack channels.
 * Four colored bars forming a hash pattern.
 */
function SlackIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      {/* Simplified Slack logo -- four colored bars forming a hash */}
      <rect x="7" y="2" width="2.5" height="8" rx="1.25" fill={color} />
      <rect x="11" y="10" width="2.5" height="8" rx="1.25" fill={color} />
      <rect x="2" y="10.5" width="8" height="2.5" rx="1.25" fill={color} />
      <rect x="10" y="7" width="8" height="2.5" rx="1.25" fill={color} />
    </svg>
  );
}

/**
 * REST (default/fallback): Gear icon -- represents generic API connector.
 */
function RestIcon({ color, size }: Readonly<{ color: string; size: number }>) {
  // Simplified gear: outer toothed ring + inner circle
  const teeth = 6;
  const outerR = 10;
  const toothDepth = 2;

  const pathParts: string[] = [];
  for (let i = 0; i < teeth; i++) {
    const startAngle = (i * 360) / teeth;
    const midAngle = startAngle + 360 / teeth / 2;
    const endAngle = startAngle + 360 / teeth;

    const toRad = (deg: number) => (deg - 90) * (Math.PI / 180);

    // Outer tooth point
    const ox1 = 12 + (outerR + toothDepth) * Math.cos(toRad(startAngle + 10));
    const oy1 = 12 + (outerR + toothDepth) * Math.sin(toRad(startAngle + 10));
    const ox2 = 12 + (outerR + toothDepth) * Math.cos(toRad(midAngle - 10));
    const oy2 = 12 + (outerR + toothDepth) * Math.sin(toRad(midAngle - 10));

    // Inner valley points
    const ix1 = 12 + outerR * Math.cos(toRad(midAngle + 5));
    const iy1 = 12 + outerR * Math.sin(toRad(midAngle + 5));
    const ix2 = 12 + outerR * Math.cos(toRad(endAngle - 5));
    const iy2 = 12 + outerR * Math.sin(toRad(endAngle - 5));

    // Start of tooth
    const sx = 12 + outerR * Math.cos(toRad(startAngle + 5));
    const sy = 12 + outerR * Math.sin(toRad(startAngle + 5));

    if (i === 0) {
      pathParts.push(`M${sx},${sy}`);
    } else {
      pathParts.push(`L${sx},${sy}`);
    }
    pathParts.push(`L${ox1},${oy1}`);
    pathParts.push(`L${ox2},${oy2}`);
    pathParts.push(`L${ix1},${iy1}`);
    pathParts.push(`L${ix2},${iy2}`);
  }
  pathParts.push('Z');

  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path
        d={pathParts.join(' ')}
        fill={color}
        fillOpacity="0.15"
        stroke={color}
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <circle cx="12" cy="12" r="3.5" fill={color} fillOpacity="0.4" stroke={color} strokeWidth="1" />
    </svg>
  );
}

// ============================================================================
// Main Component
// ============================================================================

export function ConnectorIcon({ connectorType, size = 16, className }: Readonly<ConnectorIconProps>) {
  const normalizedType = connectorType.toLowerCase();
  const color = CONNECTOR_COLORS[normalizedType] || CONNECTOR_COLORS.rest;

  const iconProps = { color, size };

  let icon: React.ReactElement;
  switch (normalizedType) {
    case 'kubernetes':
      icon = <KubernetesIcon {...iconProps} />;
      break;
    case 'vmware':
      icon = <VmwareIcon {...iconProps} />;
      break;
    case 'gcp':
      icon = <GcpIcon {...iconProps} />;
      break;
    case 'proxmox':
      icon = <ProxmoxIcon {...iconProps} />;
      break;
    case 'prometheus':
      icon = <PrometheusIcon {...iconProps} />;
      break;
    case 'loki':
      icon = <LokiIcon {...iconProps} />;
      break;
    case 'tempo':
      icon = <TempoIcon {...iconProps} />;
      break;
    case 'alertmanager':
      icon = <AlertmanagerIcon {...iconProps} />;
      break;
    case 'jira':
      icon = <JiraIcon {...iconProps} />;
      break;
    case 'confluence':
      icon = <ConfluenceIcon {...iconProps} />;
      break;
    case 'email':
      icon = <EmailIcon {...iconProps} />;
      break;
    case 'azure':
      icon = <AzureIcon {...iconProps} />;
      break;
    case 'aws':
      icon = <AwsIcon {...iconProps} />;
      break;
    case 'slack':
      icon = <SlackIcon {...iconProps} />;
      break;
    default:
      icon = <RestIcon {...iconProps} />;
      break;
  }

  if (className) {
    return <span className={className}>{icon}</span>;
  }

  return icon;
}
