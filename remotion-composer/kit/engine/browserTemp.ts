// Where Chrome keeps its temporary files while the kit drives it.
//
// Remotion starts Chrome with --disable-dev-shm-usage, so Chrome keeps its shared memory, the
// frames it hands between its own processes, in files under its temp folder. In a job that
// folder is on disk, and a 30 second 1080x1920 video sends 7 to 8 GB through it: the disk then
// sets the pace of the render. The same finished project rendered in 115 to 139 s with the
// folder on disk and in 29 to 31 s with it in memory, every frame the same (2026-11-10).
// So Chrome gets a folder of its own on the memory filesystem when there is one with room.
// Only Chrome does: the kit's and Remotion's own temp files stay where they are.

export const MEMORY_DIR = '/dev/shm';
/** What statfs reports as the type of a memory filesystem (tmpfs). */
export const TMPFS_MAGIC = 0x01021994;
/** A 1080x1920 render held 1.1 GB there at most. With less room than this Chrome stays on
 *  disk rather than run out in the middle of a render. */
export const ROOM_BYTES = 2 * 1024 ** 3;

export type Probe = {
  platform: string;
  env: Record<string, string | undefined>;
  statfs: (dir: string) => {type: number | bigint; bavail: number | bigint; bsize: number | bigint};
  mkdtemp: (prefix: string) => string;
};

/** A new folder for Chrome on the memory filesystem, or null to leave Chrome where it is:
 *  not Linux, switched off (KIT_CHROME_TEMP=disk), no memory filesystem at /dev/shm, too
 *  little room there, or no folder could be made in it. */
export function chromeTempDir(probe: Probe): string | null {
  if (probe.platform !== 'linux' || probe.env.KIT_CHROME_TEMP === 'disk') return null;
  try {
    const st = probe.statfs(MEMORY_DIR);
    if (Number(st.type) !== TMPFS_MAGIC) return null;
    if (Number(st.bavail) * Number(st.bsize) < ROOM_BYTES) return null;
    return probe.mkdtemp(`${MEMORY_DIR}/kit-chrome-`);
  } catch {
    return null;
  }
}

/** Runs `start` with TMPDIR set to `dir`, then puts the environment back: a program started
 *  inside inherits the folder, nothing started later does. With no folder it only runs. */
export async function withTempDir<T>(env: Record<string, string | undefined>, dir: string | null, start: () => Promise<T>): Promise<T> {
  if (!dir) return start();
  const before = env.TMPDIR;
  env.TMPDIR = dir;
  try {
    return await start();
  } finally {
    if (before === undefined) delete env.TMPDIR;
    else env.TMPDIR = before;
  }
}
