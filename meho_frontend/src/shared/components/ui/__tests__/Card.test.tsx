// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for Card component
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Card, CardHeader, CardContent, CardFooter } from '../Card';

describe('Card', () => {
  describe('rendering', () => {
    it('renders children correctly', () => {
      render(<Card>Card content</Card>);
      expect(screen.getByText('Card content')).toBeInTheDocument();
    });

    it('applies custom className', () => {
      const { container } = render(<Card className="custom-class">Content</Card>);
      expect(container.firstChild).toHaveClass('custom-class');
    });

    it('has rounded corners by default', () => {
      const { container } = render(<Card>Content</Card>);
      expect(container.firstChild).toHaveClass('rounded-xl');
    });
  });

  describe('variants', () => {
    it('applies default variant styles', () => {
      const { container } = render(<Card variant="default">Default</Card>);
      expect(container.firstChild).toHaveClass('bg-surface', 'border', 'border-border');
    });

    it('applies elevated variant styles', () => {
      const { container } = render(<Card variant="elevated">Elevated</Card>);
      expect(container.firstChild).toHaveClass('bg-surface', 'shadow-lg');
    });

    it('applies outlined variant styles', () => {
      const { container } = render(<Card variant="outlined">Outlined</Card>);
      expect(container.firstChild).toHaveClass('bg-transparent', 'border', 'border-border');
    });

    it('applies ghost variant styles', () => {
      const { container } = render(<Card variant="ghost">Ghost</Card>);
      expect(container.firstChild).toHaveClass('bg-transparent');
    });
  });

  describe('padding', () => {
    it('applies no padding when padding is none', () => {
      const { container } = render(<Card padding="none">No Padding</Card>);
      expect(container.firstChild).not.toHaveClass('p-3', 'p-4', 'p-6');
    });

    it('applies small padding', () => {
      const { container } = render(<Card padding="sm">Small</Card>);
      expect(container.firstChild).toHaveClass('p-3');
    });

    it('applies medium padding by default', () => {
      const { container } = render(<Card>Medium</Card>);
      expect(container.firstChild).toHaveClass('p-4');
    });

    it('applies large padding', () => {
      const { container } = render(<Card padding="lg">Large</Card>);
      expect(container.firstChild).toHaveClass('p-6');
    });
  });

  describe('hoverable', () => {
    it('applies hover styles when hoverable is true', () => {
      const { container } = render(<Card hoverable>Hoverable</Card>);
      expect(container.firstChild).toHaveClass('cursor-pointer');
    });

    it('does not apply hover styles by default', () => {
      const { container } = render(<Card>Non-hoverable</Card>);
      expect(container.firstChild).not.toHaveClass('cursor-pointer');
    });

    it('calls onClick when hoverable card is clicked', async () => {
      const onClick = vi.fn();
      render(<Card hoverable onClick={onClick}>Clickable</Card>);

      await userEvent.click(screen.getByText('Clickable'));

      expect(onClick).toHaveBeenCalledOnce();
    });
  });

  describe('ref forwarding', () => {
    it('forwards ref to div element', () => {
      const ref = { current: null as HTMLDivElement | null };
      render(<Card ref={ref}>Ref Test</Card>);
      expect(ref.current).toBeInstanceOf(HTMLDivElement);
    });
  });
});

describe('CardHeader', () => {
  it('renders title', () => {
    render(
      <Card>
        <CardHeader title="Header Title" />
      </Card>
    );
    expect(screen.getByText('Header Title')).toBeInTheDocument();
  });

  it('renders subtitle', () => {
    render(
      <Card>
        <CardHeader title="Title" subtitle="Subtitle text" />
      </Card>
    );
    expect(screen.getByText('Subtitle text')).toBeInTheDocument();
  });

  it('renders action slot', () => {
    render(
      <Card>
        <CardHeader title="Title" action={<button>Action</button>} />
      </Card>
    );
    expect(screen.getByRole('button', { name: 'Action' })).toBeInTheDocument();
  });

  it('renders children', () => {
    render(
      <Card>
        <CardHeader>
          <span>Custom content</span>
        </CardHeader>
      </Card>
    );
    expect(screen.getByText('Custom content')).toBeInTheDocument();
  });

  it('applies correct title styling', () => {
    render(
      <Card>
        <CardHeader title="Styled Title" />
      </Card>
    );
    const title = screen.getByText('Styled Title');
    expect(title).toHaveClass('text-lg', 'font-semibold', 'text-text-primary');
  });

  it('applies correct subtitle styling', () => {
    render(
      <Card>
        <CardHeader title="Title" subtitle="Styled Subtitle" />
      </Card>
    );
    const subtitle = screen.getByText('Styled Subtitle');
    expect(subtitle).toHaveClass('text-sm', 'text-text-secondary');
  });

  it('forwards ref', () => {
    const ref = { current: null as HTMLDivElement | null };
    render(
      <Card>
        <CardHeader ref={ref} title="Ref Test" />
      </Card>
    );
    expect(ref.current).toBeInstanceOf(HTMLDivElement);
  });
});

describe('CardContent', () => {
  it('renders children', () => {
    render(
      <Card>
        <CardContent>Content goes here</CardContent>
      </Card>
    );
    expect(screen.getByText('Content goes here')).toBeInTheDocument();
  });

  it('applies custom className', () => {
    render(
      <Card>
        <CardContent className="custom-content">Content</CardContent>
      </Card>
    );
    expect(screen.getByText('Content')).toHaveClass('custom-content');
  });

  it('forwards ref', () => {
    const ref = { current: null as HTMLDivElement | null };
    render(
      <Card>
        <CardContent ref={ref}>Ref Test</CardContent>
      </Card>
    );
    expect(ref.current).toBeInstanceOf(HTMLDivElement);
  });
});

describe('CardFooter', () => {
  it('renders children', () => {
    render(
      <Card>
        <CardFooter>
          <button>Cancel</button>
          <button>Save</button>
        </CardFooter>
      </Card>
    );
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
  });

  it('applies flex and border styling', () => {
    render(
      <Card>
        <CardFooter data-testid="footer">
          <button>Action</button>
        </CardFooter>
      </Card>
    );
    const footer = screen.getByTestId('footer');
    expect(footer).toHaveClass('flex', 'items-center', 'border-t', 'border-border');
  });

  it('applies custom className', () => {
    render(
      <Card>
        <CardFooter className="custom-footer">Footer</CardFooter>
      </Card>
    );
    expect(screen.getByText('Footer')).toHaveClass('custom-footer');
  });

  it('forwards ref', () => {
    const ref = { current: null as HTMLDivElement | null };
    render(
      <Card>
        <CardFooter ref={ref}>Ref Test</CardFooter>
      </Card>
    );
    expect(ref.current).toBeInstanceOf(HTMLDivElement);
  });
});

describe('Card composition', () => {
  it('renders complete card with all subcomponents', () => {
    render(
      <Card>
        <CardHeader
          title="Complete Card"
          subtitle="With all parts"
          action={<button>Edit</button>}
        />
        <CardContent>Main content area</CardContent>
        <CardFooter>
          <button>Cancel</button>
          <button>Save</button>
        </CardFooter>
      </Card>
    );

    expect(screen.getByText('Complete Card')).toBeInTheDocument();
    expect(screen.getByText('With all parts')).toBeInTheDocument();
    expect(screen.getByText('Main content area')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
  });
});

