// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tabs Component Tests
 * 
 * Tests for both Tabs (with animation) and SimpleTabs (without animation) variants.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Tabs, SimpleTabs } from '../Tabs';

// Mock motion/react to avoid animation issues in tests
vi.mock('motion/react', () => ({
  motion: {
    div: ({ children, ...props }: { children?: React.ReactNode; [key: string]: unknown }) => (
      <div {...props}>{children}</div>
    ),
  },
}));

describe('Tabs', () => {
  const baseTabs = [
    { id: 'tab1', label: 'Tab 1', content: <div>Content 1</div> },
    { id: 'tab2', label: 'Tab 2', content: <div>Content 2</div> },
    { id: 'tab3', label: 'Tab 3', content: <div>Content 3</div> },
  ];

  describe('rendering', () => {
    it('renders all tab labels', () => {
      render(<Tabs tabs={baseTabs} />);
      
      expect(screen.getByText('Tab 1')).toBeInTheDocument();
      expect(screen.getByText('Tab 2')).toBeInTheDocument();
      expect(screen.getByText('Tab 3')).toBeInTheDocument();
    });

    it('renders content for active tab', () => {
      render(<Tabs tabs={baseTabs} defaultTab="tab2" />);
      
      expect(screen.getByText('Content 2')).toBeInTheDocument();
    });

    it('selects first tab by default when no defaultTab provided', () => {
      render(<Tabs tabs={baseTabs} />);
      
      expect(screen.getByText('Content 1')).toBeInTheDocument();
    });

    it('applies custom className', () => {
      const { container } = render(<Tabs tabs={baseTabs} className="custom-class" />);
      
      expect(container.firstChild).toHaveClass('custom-class');
    });

    it('renders icons when provided', () => {
      const tabsWithIcons = [
        { id: 'tab1', label: 'Tab 1', icon: <span data-testid="icon1">Icon</span>, content: <div>Content</div> },
      ];
      
      render(<Tabs tabs={tabsWithIcons} />);
      
      expect(screen.getByTestId('icon1')).toBeInTheDocument();
    });
  });

  describe('tab switching', () => {
    it('switches content when clicking a tab', () => {
      render(<Tabs tabs={baseTabs} />);
      
      // Initially tab 1 content is shown
      expect(screen.getByText('Content 1')).toBeInTheDocument();
      
      // Click tab 2
      fireEvent.click(screen.getByText('Tab 2'));
      
      // Now tab 2 content is shown
      expect(screen.getByText('Content 2')).toBeInTheDocument();
    });

    it('calls onChange callback when tab changes', () => {
      const onChange = vi.fn();
      render(<Tabs tabs={baseTabs} onChange={onChange} />);
      
      fireEvent.click(screen.getByText('Tab 2'));
      
      expect(onChange).toHaveBeenCalledWith('tab2');
    });

    it('does not call onChange on initial render', () => {
      const onChange = vi.fn();
      render(<Tabs tabs={baseTabs} onChange={onChange} />);
      
      expect(onChange).not.toHaveBeenCalled();
    });
  });

  describe('disabled state', () => {
    it('does not switch to disabled tab when clicked', () => {
      const tabsWithDisabled = [
        { id: 'tab1', label: 'Tab 1', content: <div>Content 1</div> },
        { id: 'tab2', label: 'Tab 2', content: <div>Content 2</div>, disabled: true },
      ];
      
      render(<Tabs tabs={tabsWithDisabled} />);
      
      fireEvent.click(screen.getByText('Tab 2'));
      
      // Should still show content 1
      expect(screen.getByText('Content 1')).toBeInTheDocument();
    });

    it('applies disabled styling to disabled tabs', () => {
      const tabsWithDisabled = [
        { id: 'tab1', label: 'Tab 1', content: <div>Content 1</div> },
        { id: 'tab2', label: 'Tab 2', content: <div>Content 2</div>, disabled: true },
      ];
      
      render(<Tabs tabs={tabsWithDisabled} />);
      
      const disabledTab = screen.getByRole('tab', { name: 'Tab 2' });
      expect(disabledTab).toBeDisabled();
    });

    it('does not call onChange when clicking disabled tab', () => {
      const onChange = vi.fn();
      const tabsWithDisabled = [
        { id: 'tab1', label: 'Tab 1', content: <div>Content 1</div> },
        { id: 'tab2', label: 'Tab 2', content: <div>Content 2</div>, disabled: true },
      ];
      
      render(<Tabs tabs={tabsWithDisabled} onChange={onChange} />);
      
      fireEvent.click(screen.getByText('Tab 2'));
      
      expect(onChange).not.toHaveBeenCalled();
    });
  });

  describe('accessibility', () => {
    it('has role="tablist" on the tab container', () => {
      render(<Tabs tabs={baseTabs} />);
      
      expect(screen.getByRole('tablist')).toBeInTheDocument();
    });

    it('has role="tab" on each tab button', () => {
      render(<Tabs tabs={baseTabs} />);
      
      const tabs = screen.getAllByRole('tab');
      expect(tabs).toHaveLength(3);
    });

    it('has role="tabpanel" on the content area', () => {
      render(<Tabs tabs={baseTabs} />);
      
      expect(screen.getByRole('tabpanel')).toBeInTheDocument();
    });

    it('sets aria-selected correctly', () => {
      render(<Tabs tabs={baseTabs} defaultTab="tab2" />);
      
      const tab1 = screen.getByRole('tab', { name: 'Tab 1' });
      const tab2 = screen.getByRole('tab', { name: 'Tab 2' });
      
      expect(tab1).toHaveAttribute('aria-selected', 'false');
      expect(tab2).toHaveAttribute('aria-selected', 'true');
    });

    it('updates aria-selected when switching tabs', () => {
      render(<Tabs tabs={baseTabs} />);
      
      const tab1 = screen.getByRole('tab', { name: 'Tab 1' });
      const tab2 = screen.getByRole('tab', { name: 'Tab 2' });
      
      expect(tab1).toHaveAttribute('aria-selected', 'true');
      expect(tab2).toHaveAttribute('aria-selected', 'false');
      
      fireEvent.click(tab2);
      
      expect(tab1).toHaveAttribute('aria-selected', 'false');
      expect(tab2).toHaveAttribute('aria-selected', 'true');
    });

    it('has correct aria-controls on tabs', () => {
      render(<Tabs tabs={baseTabs} defaultTab="tab1" />);
      
      const tab1 = screen.getByRole('tab', { name: 'Tab 1' });
      expect(tab1).toHaveAttribute('aria-controls', 'tabpanel-tab1');
    });

    it('has matching id on tabpanel', () => {
      render(<Tabs tabs={baseTabs} defaultTab="tab1" />);
      
      const tabpanel = screen.getByRole('tabpanel');
      expect(tabpanel).toHaveAttribute('id', 'tabpanel-tab1');
    });

    it('sets tabIndex correctly for active/inactive tabs', () => {
      render(<Tabs tabs={baseTabs} defaultTab="tab2" />);
      
      const tab1 = screen.getByRole('tab', { name: 'Tab 1' });
      const tab2 = screen.getByRole('tab', { name: 'Tab 2' });
      
      expect(tab1).toHaveAttribute('tabIndex', '-1');
      expect(tab2).toHaveAttribute('tabIndex', '0');
    });
  });
});

describe('SimpleTabs', () => {
  const baseTabs = [
    { id: 'tab1', label: 'Tab 1', content: <div>Content 1</div> },
    { id: 'tab2', label: 'Tab 2', content: <div>Content 2</div> },
  ];

  describe('rendering', () => {
    it('renders all tab labels', () => {
      render(<SimpleTabs tabs={baseTabs} />);
      
      expect(screen.getByText('Tab 1')).toBeInTheDocument();
      expect(screen.getByText('Tab 2')).toBeInTheDocument();
    });

    it('renders content for active tab', () => {
      render(<SimpleTabs tabs={baseTabs} defaultTab="tab2" />);
      
      expect(screen.getByText('Content 2')).toBeInTheDocument();
    });

    it('selects first tab by default', () => {
      render(<SimpleTabs tabs={baseTabs} />);
      
      expect(screen.getByText('Content 1')).toBeInTheDocument();
    });

    it('applies custom className', () => {
      const { container } = render(<SimpleTabs tabs={baseTabs} className="custom-wrapper" />);
      
      expect(container.firstChild).toHaveClass('custom-wrapper');
    });

    it('applies tabListClassName', () => {
      render(<SimpleTabs tabs={baseTabs} tabListClassName="custom-tablist" />);
      
      expect(screen.getByRole('tablist')).toHaveClass('custom-tablist');
    });

    it('applies panelClassName', () => {
      const { container } = render(<SimpleTabs tabs={baseTabs} panelClassName="custom-panel" />);
      
      // The panel is the second child div (after tablist)
      const panel = container.querySelector('.custom-panel');
      expect(panel).toBeInTheDocument();
    });

    it('renders icons when provided', () => {
      const tabsWithIcons = [
        { id: 'tab1', label: 'Tab 1', icon: <span data-testid="icon1">Icon</span>, content: <div>Content</div> },
      ];
      
      render(<SimpleTabs tabs={tabsWithIcons} />);
      
      expect(screen.getByTestId('icon1')).toBeInTheDocument();
    });
  });

  describe('tab switching', () => {
    it('switches content when clicking a tab', () => {
      render(<SimpleTabs tabs={baseTabs} />);
      
      expect(screen.getByText('Content 1')).toBeInTheDocument();
      
      fireEvent.click(screen.getByText('Tab 2'));
      
      expect(screen.getByText('Content 2')).toBeInTheDocument();
    });

    it('calls onChange callback when tab changes', () => {
      const onChange = vi.fn();
      render(<SimpleTabs tabs={baseTabs} onChange={onChange} />);
      
      fireEvent.click(screen.getByText('Tab 2'));
      
      expect(onChange).toHaveBeenCalledWith('tab2');
    });
  });

  describe('disabled state', () => {
    it('does not switch to disabled tab', () => {
      const tabsWithDisabled = [
        { id: 'tab1', label: 'Tab 1', content: <div>Content 1</div> },
        { id: 'tab2', label: 'Tab 2', content: <div>Content 2</div>, disabled: true },
      ];
      
      render(<SimpleTabs tabs={tabsWithDisabled} />);
      
      fireEvent.click(screen.getByText('Tab 2'));
      
      expect(screen.getByText('Content 1')).toBeInTheDocument();
    });

    it('applies disabled attribute to disabled tabs', () => {
      const tabsWithDisabled = [
        { id: 'tab1', label: 'Tab 1', content: <div>Content 1</div> },
        { id: 'tab2', label: 'Tab 2', content: <div>Content 2</div>, disabled: true },
      ];
      
      render(<SimpleTabs tabs={tabsWithDisabled} />);
      
      const disabledTab = screen.getByRole('tab', { name: 'Tab 2' });
      expect(disabledTab).toBeDisabled();
    });
  });

  describe('render prop children', () => {
    it('uses children render prop when provided', () => {
      const tabs = [
        { id: 'tab1', label: 'Tab 1' },
        { id: 'tab2', label: 'Tab 2' },
      ];
      
      render(
        <SimpleTabs tabs={tabs}>
          {(activeTab) => <div data-testid="rendered">Active: {activeTab}</div>}
        </SimpleTabs>
      );
      
      expect(screen.getByTestId('rendered')).toHaveTextContent('Active: tab1');
    });

    it('updates children render prop when tab changes', () => {
      const tabs = [
        { id: 'tab1', label: 'Tab 1' },
        { id: 'tab2', label: 'Tab 2' },
      ];
      
      render(
        <SimpleTabs tabs={tabs}>
          {(activeTab) => <div data-testid="rendered">Active: {activeTab}</div>}
        </SimpleTabs>
      );
      
      fireEvent.click(screen.getByText('Tab 2'));
      
      expect(screen.getByTestId('rendered')).toHaveTextContent('Active: tab2');
    });

    it('children render prop takes precedence over tab.content', () => {
      const tabs = [
        { id: 'tab1', label: 'Tab 1', content: <div>Tab Content</div> },
      ];
      
      render(
        <SimpleTabs tabs={tabs}>
          {() => <div>Render Prop Content</div>}
        </SimpleTabs>
      );
      
      expect(screen.getByText('Render Prop Content')).toBeInTheDocument();
      expect(screen.queryByText('Tab Content')).not.toBeInTheDocument();
    });
  });

  describe('accessibility', () => {
    it('has role="tablist" on the tab container', () => {
      render(<SimpleTabs tabs={baseTabs} />);
      
      expect(screen.getByRole('tablist')).toBeInTheDocument();
    });

    it('has role="tab" on each tab button', () => {
      render(<SimpleTabs tabs={baseTabs} />);
      
      const tabs = screen.getAllByRole('tab');
      expect(tabs).toHaveLength(2);
    });

    it('sets aria-selected correctly', () => {
      render(<SimpleTabs tabs={baseTabs} defaultTab="tab2" />);
      
      const tab1 = screen.getByRole('tab', { name: 'Tab 1' });
      const tab2 = screen.getByRole('tab', { name: 'Tab 2' });
      
      expect(tab1).toHaveAttribute('aria-selected', 'false');
      expect(tab2).toHaveAttribute('aria-selected', 'true');
    });
  });

  describe('ReactNode labels', () => {
    it('supports ReactNode as label', () => {
      const tabs = [
        { id: 'tab1', label: <span data-testid="complex-label">Complex <strong>Label</strong></span>, content: <div>Content</div> },
      ];
      
      render(<SimpleTabs tabs={tabs} />);
      
      expect(screen.getByTestId('complex-label')).toBeInTheDocument();
      expect(screen.getByText('Label')).toBeInTheDocument();
    });
  });
});
