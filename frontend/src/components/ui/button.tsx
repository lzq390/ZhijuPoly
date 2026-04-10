import { cva, type VariantProps } from "class-variance-authority";
import type { ButtonHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center rounded-2xl text-sm font-medium transition-all duration-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primaryForeground shadow-[0_18px_45px_rgba(15,118,110,0.28)] hover:-translate-y-0.5 hover:bg-primary/95 hover:shadow-[0_24px_60px_rgba(15,118,110,0.34)]",
        outline:
          "border border-white/80 bg-white/80 text-foreground shadow-sm backdrop-blur hover:-translate-y-0.5 hover:border-white hover:bg-white hover:text-accentForeground",
        secondary:
          "bg-secondary text-secondaryForeground shadow-sm hover:-translate-y-0.5 hover:bg-secondary/80"
      },
      size: {
        default: "h-11 px-5 py-2",
        lg: "h-12 px-6 text-[15px]"
      }
    },
    defaultVariants: {
      variant: "default",
      size: "default"
    }
  }
);

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof buttonVariants>;

export function Button({ className, variant, size, ...props }: ButtonProps) {
  return <button className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
