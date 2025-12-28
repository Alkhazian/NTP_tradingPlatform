import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "../../lib/utils"

const badgeVariants = cva(
    "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold transition-all duration-300 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
    {
        variants: {
            variant: {
                default:
                    "border-transparent bg-primary/20 text-primary hover:bg-primary/30",
                secondary:
                    "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80",
                destructive:
                    "border-transparent bg-destructive/20 text-destructive hover:bg-destructive/30",
                outline: "text-foreground border-white/20",
                success:
                    "border-transparent bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30",
                warning:
                    "border-transparent bg-amber-500/20 text-amber-400 hover:bg-amber-500/30",
                info:
                    "border-transparent bg-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30",
            },
        },
        defaultVariants: {
            variant: "default",
        },
    }
)

export interface BadgeProps
    extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {
    pulse?: boolean;
}

function Badge({ className, variant, pulse, children, ...props }: BadgeProps) {
    return (
        <div className={cn(badgeVariants({ variant }), className)} {...props}>
            {pulse && (
                <span className={cn(
                    "w-2 h-2 rounded-full",
                    variant === "success" && "bg-emerald-400 pulse-success",
                    variant === "destructive" && "bg-red-400 pulse-danger",
                    variant === "warning" && "bg-amber-400 pulse-warning",
                    variant === "info" && "bg-cyan-400 pulse-success",
                    variant === "default" && "bg-primary pulse-success"
                )} />
            )}
            {children}
        </div>
    )
}

export { Badge, badgeVariants }
