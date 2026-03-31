// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Input Component
 * 
 * Form input with MEHO styling and proper focus states.
 */
import { forwardRef, type InputHTMLAttributes, type ReactNode } from 'react';
import { cn } from '../../lib/cn';

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  hint?: string;
  leftIcon?: ReactNode;
  rightIcon?: ReactNode;
  fullWidth?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  (
    {
      label,
      error,
      hint,
      leftIcon,
      rightIcon,
      fullWidth = false,
      className,
      id,
      ...props
    },
    ref
  ) => {
    const inputId = id || label?.toLowerCase().replace(/\s+/g, '-');
    const hasError = !!error;

    return (
      <div className={cn('flex flex-col gap-1.5', fullWidth && 'w-full')}>
        {label && (
          <label
            htmlFor={inputId}
            className="text-sm font-medium text-text-primary"
          >
            {label}
          </label>
        )}
        <div className="relative">
          {leftIcon && (
            <div className="absolute left-3 top-1/2 -translate-y-1/2 text-text-tertiary">
              {leftIcon}
            </div>
          )}
          <input
            ref={ref}
            id={inputId}
            className={cn(
              // Base styles
              'w-full bg-surface border rounded-lg',
              'px-3 py-2 text-text-primary',
              'placeholder:text-text-tertiary',
              'transition-colors duration-200',
              // Focus styles
              'focus:outline-none focus:border-primary-500 focus:ring-1 focus:ring-primary-500',
              // Error state
              hasError
                ? 'border-red-500 focus:border-red-500 focus:ring-red-500'
                : 'border-border',
              // Icons padding
              leftIcon && 'pl-10',
              rightIcon && 'pr-10',
              // Disabled state
              props.disabled && 'opacity-50 cursor-not-allowed bg-surface-hover',
              className
            )}
            {...props}
          />
          {rightIcon && (
            <div className="absolute right-3 top-1/2 -translate-y-1/2 text-text-tertiary">
              {rightIcon}
            </div>
          )}
        </div>
        {(error || hint) && (
          <p
            className={cn(
              'text-sm',
              hasError ? 'text-red-400' : 'text-text-tertiary'
            )}
          >
            {error || hint}
          </p>
        )}
      </div>
    );
  }
);

Input.displayName = 'Input';

