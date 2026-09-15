import type { StandaloneStructService } from 'ketcher-standalone';
import type { NativeEditorSession } from '../src/structure/nativeSession';

type OwnedTransport = NonNullable<ConstructorParameters<typeof StandaloneStructService>[1]>;
export function takeTransport(session: NativeEditorSession): OwnedTransport;
export function prepareInitial(): void;
export function cancelPrepared(): void;
export function cancelPreload(): void;
export function preload(): Promise<typeof import('./micro')>;
export function loadMacro(): Promise<unknown>;
