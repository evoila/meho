// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Card Component
 * 
 * A container component for grouping related content.
 * Uses MEHO's dark surface colors with purple border accent.
 */
import { forwardRef, type HTMLAttributes, type ReactNode } from 'react';
import { cn } from '../../lib/cn';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  variant?: 'default' | 'elevated' | 'outlined' | 'ghost';
  hoverable?: boolean;
  padding?: 'none' | 'sm' | 'md' | 'lg';
}

const variantStyles = {
  default: 'bg-surface border border-border',
  elevated: 'bg-surface shadow-lg',
  outlined: 'bg-transparent border border-border',
  ghost: 'bg-transparent',
};

const paddingStyles = {
  none: '',
  sm: 'p-3',
  md: 'p-4',
  lg: 'p-6',
};

export const Card = forwardRef<HTMLDivElement, CardProps>(
  (
    {
      variant = 'default',
      hoverable = false,
      padding = 'md',
      className,
      children,
      ...props
    },
    ref
  ) => {
    return (
      <div
        ref={ref}
        className={cn(
          'rounded-xl',
          variantStyles[variant],
          paddingStyles[padding],
          hoverable && 'transition-all duration-200 hover:border-border-hover hover:bg-surface-hover cursor-pointer',
          className
        )}
        {...props}
      >
        {children}
      </div>
    );
  }
);

Card.displayName = 'Card';

// Card sub-components
export interface CardHeaderProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title'> {
  title?: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
}

export const CardHeader = forwardRef<HTMLDivElement, CardHeaderProps>(
  ({ title, subtitle, action, className, children, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn('flex items-start justify-between gap-4 mb-4', className)}
        {...props}
      >
        <div className="flex-1">
          {title && (
            <h3 className="text-lg font-semibold text-text-primary">{title}</h3>
          )}
          {subtitle && (
            <p className="text-sm text-text-secondary mt-1">{subtitle}</p>
          )}
          {children}
        </div>
        {action && <div className="flex-shrink-0">{action}</div>}
      </div>
    );
  }
);

CardHeader.displayName = 'CardHeader';

export const CardContent = forwardRef<
  HTMLDivElement,
  HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => {
  return <div ref={ref} className={cn('', className)} {...props} />;
});

CardContent.displayName = 'CardContent';

export const CardFooter = forwardRef<
  HTMLDivElement,
  HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => {
  return (
    <div
      ref={ref}
      className={cn(
        'flex items-center justify-end gap-3 mt-4 pt-4 border-t border-border',
        className
      )}
      {...props}
    />
  );
});

CardFooter.displayName = 'CardFooter';

