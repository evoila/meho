// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for Input component
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Input } from '../Input';
import { Search, Eye } from 'lucide-react';

describe('Input', () => {
  describe('rendering', () => {
    it('renders input element', () => {
      render(<Input />);
      expect(screen.getByRole('textbox')).toBeInTheDocument();
    });

    it('applies custom className', () => {
      render(<Input className="custom-class" />);
      expect(screen.getByRole('textbox')).toHaveClass('custom-class');
    });

    it('renders placeholder text', () => {
      render(<Input placeholder="Enter text..." />);
      expect(screen.getByPlaceholderText('Enter text...')).toBeInTheDocument();
    });
  });

  describe('label', () => {
    it('renders label when provided', () => {
      render(<Input label="Email" />);
      expect(screen.getByText('Email')).toBeInTheDocument();
    });

    it('associates label with input', () => {
      render(<Input label="Email" />);
      const input = screen.getByLabelText('Email');
      expect(input).toBeInTheDocument();
    });

    it('generates id from label text', () => {
      render(<Input label="First Name" />);
      const input = screen.getByLabelText('First Name');
      expect(input).toHaveAttribute('id', 'first-name');
    });

    it('uses provided id over generated one', () => {
      render(<Input label="Email" id="custom-id" />);
      const input = screen.getByLabelText('Email');
      expect(input).toHaveAttribute('id', 'custom-id');
    });

    it('applies correct label styling', () => {
      render(<Input label="Email" />);
      const label = screen.getByText('Email');
      expect(label).toHaveClass('text-sm', 'font-medium', 'text-text-primary');
    });
  });

  describe('error state', () => {
    it('displays error message', () => {
      render(<Input error="This field is required" />);
      expect(screen.getByText('This field is required')).toBeInTheDocument();
    });

    it('applies error styling to input', () => {
      render(<Input error="Error" />);
      expect(screen.getByRole('textbox')).toHaveClass('border-red-500');
    });

    it('applies error styling to message', () => {
      render(<Input error="Error message" />);
      expect(screen.getByText('Error message')).toHaveClass('text-red-400');
    });

    it('prioritizes error over hint', () => {
      render(<Input error="Error" hint="Hint" />);
      expect(screen.getByText('Error')).toBeInTheDocument();
      expect(screen.queryByText('Hint')).not.toBeInTheDocument();
    });
  });

  describe('hint', () => {
    it('displays hint text', () => {
      render(<Input hint="Enter your email address" />);
      expect(screen.getByText('Enter your email address')).toBeInTheDocument();
    });

    it('applies hint styling', () => {
      render(<Input hint="Helpful hint" />);
      expect(screen.getByText('Helpful hint')).toHaveClass('text-sm', 'text-text-tertiary');
    });

    it('does not show hint when error is present', () => {
      render(<Input error="Error" hint="Hint" />);
      expect(screen.queryByText('Hint')).not.toBeInTheDocument();
    });
  });

  describe('icons', () => {
    it('renders left icon', () => {
      render(<Input leftIcon={<Search data-testid="left-icon" />} />);
      expect(screen.getByTestId('left-icon')).toBeInTheDocument();
    });

    it('renders right icon', () => {
      render(<Input rightIcon={<Eye data-testid="right-icon" />} />);
      expect(screen.getByTestId('right-icon')).toBeInTheDocument();
    });

    it('applies left padding when left icon present', () => {
      render(<Input leftIcon={<Search />} />);
      expect(screen.getByRole('textbox')).toHaveClass('pl-10');
    });

    it('applies right padding when right icon present', () => {
      render(<Input rightIcon={<Eye />} />);
      expect(screen.getByRole('textbox')).toHaveClass('pr-10');
    });

    it('renders both icons', () => {
      render(
        <Input
          leftIcon={<Search data-testid="left-icon" />}
          rightIcon={<Eye data-testid="right-icon" />}
        />
      );
      expect(screen.getByTestId('left-icon')).toBeInTheDocument();
      expect(screen.getByTestId('right-icon')).toBeInTheDocument();
    });
  });

  describe('fullWidth', () => {
    it('applies full width by default to input', () => {
      render(<Input />);
      expect(screen.getByRole('textbox')).toHaveClass('w-full');
    });

    it('applies full width to container when fullWidth is true', () => {
      const { container } = render(<Input fullWidth />);
      const wrapper = container.firstChild;
      expect(wrapper).toHaveClass('w-full');
    });
  });

  describe('disabled state', () => {
    it('is disabled when disabled prop is true', () => {
      render(<Input disabled />);
      expect(screen.getByRole('textbox')).toBeDisabled();
    });

    it('applies disabled styling', () => {
      render(<Input disabled />);
      expect(screen.getByRole('textbox')).toHaveClass('opacity-50', 'cursor-not-allowed');
    });

    it('cannot receive focus when disabled', async () => {
      render(<Input disabled />);
      const input = screen.getByRole('textbox');

      await userEvent.click(input);

      expect(document.activeElement).not.toBe(input);
    });
  });

  describe('interactions', () => {
    it('handles text input', async () => {
      render(<Input />);
      const input = screen.getByRole('textbox');

      await userEvent.type(input, 'Hello World');

      expect(input).toHaveValue('Hello World');
    });

    it('calls onChange when value changes', async () => {
      const onChange = vi.fn();
      render(<Input onChange={onChange} />);

      await userEvent.type(screen.getByRole('textbox'), 'test');

      expect(onChange).toHaveBeenCalled();
    });

    it('calls onFocus when focused', async () => {
      const onFocus = vi.fn();
      render(<Input onFocus={onFocus} />);

      await userEvent.click(screen.getByRole('textbox'));

      expect(onFocus).toHaveBeenCalled();
    });

    it('calls onBlur when blurred', async () => {
      const onBlur = vi.fn();
      render(<Input onBlur={onBlur} />);
      const input = screen.getByRole('textbox');

      await userEvent.click(input);
      await userEvent.tab();

      expect(onBlur).toHaveBeenCalled();
    });
  });

  describe('input types', () => {
    it('renders as text input by default', () => {
      render(<Input />);
      // HTML inputs without explicit type attribute are text by default
      const input = screen.getByRole('textbox');
      expect(input.tagName).toBe('INPUT');
    });

    it('renders as email input', () => {
      render(<Input type="email" />);
      expect(screen.getByRole('textbox')).toHaveAttribute('type', 'email');
    });

    it('renders as password input', () => {
      render(<Input type="password" />);
      // Password inputs don't have textbox role
      expect(document.querySelector('input[type="password"]')).toBeInTheDocument();
    });

    it('renders as number input', () => {
      render(<Input type="number" />);
      expect(screen.getByRole('spinbutton')).toBeInTheDocument();
    });
  });

  describe('forwarded props', () => {
    it('forwards value prop', () => {
      render(<Input value="test value" onChange={() => {}} />);
      expect(screen.getByRole('textbox')).toHaveValue('test value');
    });

    it('forwards name prop', () => {
      render(<Input name="email" />);
      expect(screen.getByRole('textbox')).toHaveAttribute('name', 'email');
    });

    it('forwards required prop', () => {
      render(<Input required />);
      expect(screen.getByRole('textbox')).toBeRequired();
    });

    it('forwards maxLength prop', () => {
      render(<Input maxLength={50} />);
      expect(screen.getByRole('textbox')).toHaveAttribute('maxLength', '50');
    });

    it('forwards autoComplete prop', () => {
      render(<Input autoComplete="email" />);
      expect(screen.getByRole('textbox')).toHaveAttribute('autoComplete', 'email');
    });
  });

  describe('ref forwarding', () => {
    it('forwards ref to input element', () => {
      const ref = { current: null as HTMLInputElement | null };
      render(<Input ref={ref} />);
      expect(ref.current).toBeInstanceOf(HTMLInputElement);
    });

    it('can focus input via ref', () => {
      const ref = { current: null as HTMLInputElement | null };
      render(<Input ref={ref} />);

      ref.current?.focus();

      expect(document.activeElement).toBe(ref.current);
    });
  });

  describe('styling', () => {
    it('has rounded corners', () => {
      render(<Input />);
      expect(screen.getByRole('textbox')).toHaveClass('rounded-lg');
    });

    it('has focus ring styles', () => {
      render(<Input />);
      expect(screen.getByRole('textbox')).toHaveClass(
        'focus:outline-none',
        'focus:border-primary-500',
        'focus:ring-1'
      );
    });

    it('has transition for smooth state changes', () => {
      render(<Input />);
      expect(screen.getByRole('textbox')).toHaveClass('transition-colors');
    });
  });
});

