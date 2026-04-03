// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { useState, type ReactNode } from 'react';
import { motion } from 'motion/react';
import { cn } from '../../lib/cn';

interface Tab {
  id: string;
  label: string;
  icon?: ReactNode;
  content: ReactNode;
  disabled?: boolean;
}

interface TabsProps {
  tabs: Tab[];
  defaultTab?: string;
  onChange?: (tabId: string) => void;
  className?: string;
}

/**
 * Tabs Component
 * 
 * Usage:
 * ```tsx
 * <Tabs 
 *   tabs={[
 *     { id: 'info', label: 'Info', content: <InfoPanel /> },
 *     { id: 'settings', label: 'Settings', icon: <Settings />, content: <SettingsPanel /> },
 *   ]}
 *   defaultTab="info"
 *   onChange={(id) => console.log('Tab changed:', id)}
 * />
 * ```
 */
export function Tabs({ tabs, defaultTab, onChange, className }: Readonly<TabsProps>) {
  const [activeTab, setActiveTab] = useState(defaultTab || tabs[0]?.id);

  const handleChange = (id: string) => {
    setActiveTab(id);
    onChange?.(id);
  };

  const activeContent = tabs.find((t) => t.id === activeTab)?.content;

  return (
    <div className={className}>
      {/* Tab list */}
      <div 
        className="flex border-b border-white/10" 
        role="tablist"
        aria-orientation="horizontal"
      >
        {tabs.map((tab) => {
          const isActive = activeTab === tab.id;
          
          return (
            <button
              key={tab.id}
              role="tab"
              aria-selected={isActive}
              aria-controls={`tabpanel-${tab.id}`}
              id={`tab-${tab.id}`}
              tabIndex={isActive ? 0 : -1}
              onClick={() => !tab.disabled && handleChange(tab.id)}
              disabled={tab.disabled}
              className={cn(
                'relative flex items-center gap-2 px-4 py-3 text-sm font-medium transition-colors',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
                isActive
                  ? 'text-white'
                  : 'text-text-secondary hover:text-white',
                tab.disabled && 'opacity-50 cursor-not-allowed'
              )}
            >
              {tab.icon && (
                <span className={cn(
                  'w-4 h-4',
                  isActive ? 'text-primary' : 'opacity-70'
                )}>
                  {tab.icon}
                </span>
              )}
              {tab.label}
              
              {/* Active indicator */}
              {isActive && (
                <motion.div
                  layoutId="activeTabIndicator"
                  className="absolute bottom-0 left-0 right-0 h-0.5 bg-gradient-to-r from-primary to-accent"
                  transition={{ duration: 0.2 }}
                />
              )}
            </button>
          );
        })}
      </div>

      {/* Tab panel */}
      <div 
        role="tabpanel"
        id={`tabpanel-${activeTab}`}
        aria-labelledby={`tab-${activeTab}`}
        className="mt-4"
      >
        {activeContent}
      </div>
    </div>
  );
}

/**
 * SimpleTab interface with ReactNode label type.
 * 
 * Note: Unlike `Tab.label` (string), `SimpleTab.label` is intentionally typed as
 * ReactNode to allow for more complex labels (e.g., badges, icons inline with text).
 * This is a deliberate design choice - use `Tabs` for simple string labels,
 * and `SimpleTabs` when you need richer label content.
 */
interface SimpleTab {
  id: string;
  label: ReactNode;
  icon?: ReactNode;
  content?: ReactNode;
  disabled?: boolean;
}

interface SimpleTabsProps {
  tabs: SimpleTab[];
  defaultTab?: string;
  onChange?: (tabId: string) => void;
  className?: string;
  tabListClassName?: string;
  panelClassName?: string;
  /** Render prop for content - receives active tab ID. If provided, overrides tab.content */
  children?: (activeTab: string) => ReactNode;
}

/**
 * Simple Tabs variant without animation
 * For use in places where motion.div causes issues
 * 
 * Supports two modes:
 * 1. Content via tab.content property
 * 2. Render prop via children function
 */
export function SimpleTabs({ 
  tabs, 
  defaultTab, 
  onChange, 
  className,
  tabListClassName,
  panelClassName,
  children,
}: Readonly<SimpleTabsProps>) {
  const [activeTab, setActiveTab] = useState(defaultTab || tabs[0]?.id);

  const handleChange = (id: string) => {
    setActiveTab(id);
    onChange?.(id);
  };

  // Get content either from render prop or from tab.content
  const content = children 
    ? children(activeTab) 
    : tabs.find((t) => t.id === activeTab)?.content;

  return (
    <div className={className}>
      <div className={cn("flex border-b border-white/10", tabListClassName)} role="tablist">
        {tabs.map((tab) => {
          const isActive = activeTab === tab.id;
          
          return (
            <button
              key={tab.id}
              role="tab"
              aria-selected={isActive}
              onClick={() => !tab.disabled && handleChange(tab.id)}
              disabled={tab.disabled}
              className={cn(
                'flex items-center gap-2 px-4 py-3 text-sm font-medium transition-colors',
                'border-b-2 -mb-px',
                isActive
                  ? 'text-white border-primary'
                  : 'text-text-secondary hover:text-white border-transparent',
                tab.disabled && 'opacity-50 cursor-not-allowed'
              )}
            >
              {tab.icon}
              {tab.label}
            </button>
          );
        })}
      </div>
      <div className={cn("mt-4", panelClassName)}>
        {content}
      </div>
    </div>
  );
}

