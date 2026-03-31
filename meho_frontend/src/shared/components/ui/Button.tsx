// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Button Component
 * 
 * A versatile button component using MEHO brand styling.
 * Uses Purple Plum color palette with proper hover/focus states.
 */
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { cn } from '../../lib/cn';

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost' | 'outline';
export type ButtonSize = 'sm' | 'md' | 'lg';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  isLoading?: boolean;
  leftIcon?: ReactNode;
  rightIcon?: ReactNode;
  fullWidth?: boolean;
}

const variantStyles: Record<ButtonVariant, string> = {
  primary: 'bg-primary-800 text-white hover:bg-primary-500 focus:ring-primary-500',
  secondary: 'bg-surface-hover text-text-primary border border-border hover:bg-surface-active hover:border-border-hover',
  danger: 'bg-red-500 text-white hover:bg-red-400 focus:ring-red-500',
  ghost: 'bg-transparent text-text-secondary hover:bg-surface-hover hover:text-text-primary',
  outline: 'bg-transparent text-primary-500 border border-primary-500 hover:bg-primary-500/10',
};

const sizeStyles: Record<ButtonSize, string> = {
  sm: 'px-3 py-1.5 text-sm',
  md: 'px-4 py-2 text-base',
  lg: 'px-6 py-3 text-lg',
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      variant = 'primary',
      size = 'md',
      isLoading = false,
      leftIcon,
      rightIcon,
      fullWidth = false,
      disabled,
      className,
      children,
      ...props
    },
    ref
  ) => {
    const isDisabled = disabled || isLoading;

    return (
      <button
        ref={ref}
        disabled={isDisabled}
        className={cn(
          // Base styles
          'inline-flex items-center justify-center gap-2',
          'font-medium rounded-lg',
          'transition-colors duration-200',
          'focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-offset-background',
          // Variant and size
          variantStyles[variant],
          sizeStyles[size],
          // Full width
          fullWidth && 'w-full',
          // Disabled state
          isDisabled && 'opacity-50 cursor-not-allowed',
          className
        )}
        {...props}
      >
        {isLoading ? (
          <Spinner size={size === 'lg' ? 'md' : 'sm'} />
        ) : (
          leftIcon
        )}
        {children}
        {!isLoading && rightIcon}
      </button>
    );
  }
);

Button.displayName = 'Button';

// Inline Spinner for loading state
interface SpinnerProps {
  size?: 'sm' | 'md';
}

function Spinner({ size = 'sm' }: SpinnerProps) {
  return (
    <svg
      className={cn(
        'animate-spin',
        size === 'sm' ? 'h-4 w-4' : 'h-5 w-5'
      )}
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle
        className="opacity-25"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
      />
    </svg>
  );
}

