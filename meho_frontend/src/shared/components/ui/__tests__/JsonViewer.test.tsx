// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * JsonViewer Tests
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { JsonViewer } from '../JsonViewer';

describe('JsonViewer', () => {
  it('renders simple object', () => {
    const { container } = render(<JsonViewer data={{ name: 'test' }} />);
    expect(container.textContent).toContain('name');
    expect(container.textContent).toContain('test');
  });

  it('renders array', () => {
    const { container } = render(<JsonViewer data={[1, 2, 3]} />);
    expect(container.textContent).toContain('1');
    expect(container.textContent).toContain('2');
    expect(container.textContent).toContain('3');
  });

  it('renders line numbers by default', () => {
    const { container } = render(<JsonViewer data={{ a: 1, b: 2 }} />);
    expect(container.textContent).toContain('1');
  });

  it('hides line numbers when showLineNumbers is false', () => {
    const { container } = render(
      <JsonViewer data={{ a: 1 }} showLineNumbers={false} />
    );
    // Should not have the line number gutter
    expect(container.querySelector('.border-r')).toBeNull();
  });

  it('renders plain string as-is when not valid JSON', () => {
    render(<JsonViewer data="Hello, world!" />);
    // Plain strings are rendered without quotes
    expect(screen.getByText('Hello, world!')).toBeInTheDocument();
  });

  it('renders null value', () => {
    const { container } = render(<JsonViewer data={{ value: null }} />);
    expect(container.textContent).toContain('null');
  });

  it('renders boolean values', () => {
    const { container } = render(<JsonViewer data={{ active: true, deleted: false }} />);
    expect(container.textContent).toContain('true');
    expect(container.textContent).toContain('false');
  });

  it('renders nested objects', () => {
    const data = {
      user: {
        name: 'John',
        address: {
          city: 'NYC',
        },
      },
    };
    const { container } = render(<JsonViewer data={data} />);
    expect(container.textContent).toContain('user');
    expect(container.textContent).toContain('name');
    expect(container.textContent).toContain('John');
    expect(container.textContent).toContain('city');
    expect(container.textContent).toContain('NYC');
  });

  it('applies custom className', () => {
    const { container } = render(<JsonViewer data={{}} className="custom-class" />);
    expect(container.firstChild).toHaveClass('custom-class');
  });

  it('applies maxHeight style when provided', () => {
    const { container } = render(<JsonViewer data={{}} maxHeight="200px" />);
    expect(container.firstChild).toHaveStyle({ maxHeight: '200px' });
  });

  it('handles empty object', () => {
    const { container } = render(<JsonViewer data={{}} />);
    expect(container.textContent).toContain('{}');
  });

  it('handles empty array', () => {
    const { container } = render(<JsonViewer data={[]} />);
    expect(container.textContent).toContain('[]');
  });

  it('renders numbers with correct highlighting', () => {
    const { container } = render(<JsonViewer data={{ count: 42, price: 19.99 }} />);
    expect(container.textContent).toContain('42');
    expect(container.textContent).toContain('19.99');
  });
});
