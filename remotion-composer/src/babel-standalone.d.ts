// @babel/standalone ships without TypeScript types. Declare it as an ambient
// module so the runtime-compiled CustomComposition can import it without
// breaking `tsc --noEmit`. Only the subset we use is loosely typed as any.
declare module "@babel/standalone";
