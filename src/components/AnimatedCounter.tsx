import { useEffect, useState, useRef } from "react";

type AnimatedCounterProps = {
  value: number;
  prefix?: string;
  suffix?: string;
  duration?: number;
  decimals?: number;
  formatter?: (value: number) => string;
  className?: string;
};

export function AnimatedCounter({
  value,
  prefix = "",
  suffix = "",
  duration = 800,
  decimals = 0,
  formatter,
  className = "",
}: AnimatedCounterProps) {
  const [displayValue, setDisplayValue] = useState(0);
  const startTime = useRef<number | null>(null);
  const rafId = useRef<number | null>(null);
  const startValue = useRef(0);
  const [hasAnimated, setHasAnimated] = useState(false);

  useEffect(() => {
    // Reset animation on value change
    startValue.current = displayValue;
    startTime.current = null;
    setHasAnimated(false);

    const animate = (timestamp: number) => {
      if (!startTime.current) startTime.current = timestamp;
      const elapsed = timestamp - startTime.current;
      const progress = Math.min(elapsed / duration, 1);
      
      // Ease out cubic
      const eased = 1 - Math.pow(1 - progress, 3);
      const current = startValue.current + (value - startValue.current) * eased;
      
      setDisplayValue(current);
      setHasAnimated(progress >= 1);

      if (progress < 1) {
        rafId.current = requestAnimationFrame(animate);
      }
    };

    rafId.current = requestAnimationFrame(animate);
    return () => {
      if (rafId.current) cancelAnimationFrame(rafId.current);
    };
  }, [value, duration]);

  const formatted = formatter
    ? formatter(displayValue)
    : displayValue.toLocaleString(undefined, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      });

  return (
    <span className={`${className} ${hasAnimated ? 'animate-count-in' : ''}`}>
      {prefix}{formatted}{suffix}
    </span>
  );
}