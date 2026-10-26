import { useState } from 'react';
import { useMediaQuery } from '../hooks/useMediaQuery';
import {
  FONT_SIZES,
  WIDTHS,
  type FontSize,
  type useReaderSettings,
  type Width,
} from '../hooks/useReaderSettings';
import { CUSTOM_ID_PREFIX, GENERICS, cleanFamily, type Generic } from '../lib/readerFonts';
import { Dialog } from './Dialog';
import { APP_THEME_ID, MAX_NAME, MIN_CONTRAST, contrastRatio } from '../lib/readerThemes';

type Reader = ReturnType<typeof useReaderSettings>;

const FONT_LABEL: Record<FontSize, string> = {
  small: 'Small text',
  medium: 'Medium text',
  large: 'Large text',
  xlarge: 'Extra large text',
};
const FONT_SAMPLE: Record<FontSize, string> = {
  small: 'text-sm',
  medium: 'text-base',
  large: 'text-xl',
  xlarge: 'text-2xl',
};
const WIDTH_LABEL: Record<Width, string> = { narrow: 'Narrow', medium: 'Medium', wide: 'Wide' };
const GENERIC_LABEL: Record<Generic, string> = {
  serif: 'Serif',
  'sans-serif': 'Sans-serif',
  monospace: 'Monospace',
};

// No text size here: the text-size buttons set their own, the others add `text-sm`.
const choice = (selected: boolean) =>
  `min-h-11 rounded-lg border px-3 font-semibold ${
    selected ? 'border-accent ring-2 ring-accent' : 'border-line'
  }`;
const field = 'min-h-11 rounded-lg border border-line bg-surface px-3 text-ink';
const small = 'min-h-11 rounded-lg border border-line px-4 text-sm font-semibold';
const primary =
  'min-h-11 rounded-lg bg-accent px-4 text-sm font-semibold text-accent-ink disabled:opacity-40';

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm font-semibold">{title}</p>
      {children}
    </div>
  );
}

const DESKTOP = '(min-width: 720px)';
const DEFAULT_PAGE = '#fbf8f1';
const DEFAULT_INK = '#26231f';

/**
 * The new-theme editor, a dialog of its own over the settings (a bottom sheet on a phone). The
 * owner renders it while it is open and unmounts it on `onClose`.
 */
export function ThemeEditorDialog({ reader, onClose }: { reader: Reader; onClose: () => void }) {
  const wide = useMediaQuery(DESKTOP);
  const [name, setName] = useState('');
  const [page, setPage] = useState(DEFAULT_PAGE);
  const [ink, setInk] = useState(DEFAULT_INK);
  const [error, setError] = useState<string | null>(null);
  const ratio = contrastRatio(page, ink);

  function save() {
    if (reader.saveTheme({ name, page, ink })) onClose();
    else setError('Too many custom themes. Delete one first.');
  }

  return (
    <Dialog title="New theme" sheet={!wide} onClose={onClose}>
      <div className="mt-3 flex flex-col gap-3">
        <input
          aria-label="Theme name"
          data-autofocus
          maxLength={MAX_NAME}
          placeholder="Theme name"
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
          className={field}
        />
        <div className="flex gap-3">
          <label className="flex flex-1 flex-col gap-1 text-sm">
            Background
            <input
              type="color"
              aria-label="Background colour"
              value={page}
              onChange={(event) => {
                setPage(event.target.value);
              }}
              className="h-11 w-full rounded-lg border border-line"
            />
          </label>
          <label className="flex flex-1 flex-col gap-1 text-sm">
            Text
            <input
              type="color"
              aria-label="Text colour"
              value={ink}
              onChange={(event) => {
                setInk(event.target.value);
              }}
              className="h-11 w-full rounded-lg border border-line"
            />
          </label>
        </div>
        <p
          style={{ background: page, color: ink }}
          className="rounded-lg border border-line px-3 py-2 text-base"
        >
          The quick brown fox jumps over the lazy dog.
        </p>
        <p className="text-xs text-ink-muted">{`Contrast ${ratio.toFixed(1)} : 1`}</p>
        {ratio < MIN_CONTRAST && (
          <p className="text-xs font-semibold text-gold-ink">
            Low contrast: the text may be hard to read.
          </p>
        )}
        {error !== null && (
          <p role="alert" className="text-sm font-semibold text-danger">
            {error}
          </p>
        )}
        <div className="mt-1 flex gap-2 border-t border-line pt-4">
          <button type="button" onClick={onClose} className={small}>
            Cancel
          </button>
          <button
            type="button"
            disabled={name.trim() === ''}
            onClick={save}
            className={`${primary} flex-1`}
          >
            Save theme
          </button>
        </div>
      </div>
    </Dialog>
  );
}

const GRID = 'grid grid-cols-4 gap-2';
const tile = (selected: boolean) =>
  `flex flex-col items-stretch gap-1 rounded-lg p-1 text-center ${
    selected ? 'ring-2 ring-accent' : ''
  }`;
const sample =
  'flex h-11 items-center justify-center rounded-md border border-line text-lg font-semibold';
const caption = 'truncate px-0.5 text-[11px] leading-tight text-ink-muted';

/** A small box with "Aa" (in the theme's colours or the font's face) and the name under it. */
function Tile({
  name,
  selected,
  onPick,
  style,
  sampleStyle,
}: {
  name: string;
  selected: boolean;
  onPick: () => void;
  style?: React.CSSProperties;
  sampleStyle?: React.CSSProperties;
}) {
  return (
    <button
      type="button"
      aria-label={name}
      aria-pressed={selected}
      onClick={onPick}
      style={style}
      className={tile(selected)}
    >
      <span style={sampleStyle} className={sample}>
        Aa
      </span>
      <span className={caption}>{name}</span>
    </button>
  );
}

/** The dashed tile that starts creating a theme or a font. */
function AddTile({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button type="button" aria-label={label} onClick={onClick} className={tile(false)}>
      <span className={`${sample} border-dashed text-ink-muted`}>+</span>
      <span className={caption}>{label}</span>
    </button>
  );
}

const deleteButton =
  'min-h-11 self-start rounded-lg border border-line px-4 text-sm font-semibold text-danger';

function FontEditor({ reader, onDone }: { reader: Reader; onDone: () => void }) {
  const [family, setFamily] = useState('');
  const [generic, setGeneric] = useState<Generic>('serif');
  const [error, setError] = useState<string | null>(null);

  function save() {
    if (cleanFamily(family) === null) {
      setError('Use letters, numbers, spaces, - . and _ only (up to 60 characters).');
    } else if (reader.saveFont({ family, generic })) {
      onDone();
    } else {
      setError('Too many custom fonts. Delete one first.');
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-line p-3">
      <input
        aria-label="Font family"
        placeholder="Font family, e.g. Literata"
        value={family}
        onChange={(event) => {
          setFamily(event.target.value);
        }}
        className={field}
      />
      <label className="flex flex-col gap-1 text-sm">
        Fallback
        <select
          aria-label="Fallback"
          value={generic}
          onChange={(event) => {
            const picked = GENERICS.find((g) => g === event.target.value);
            if (picked) setGeneric(picked);
          }}
          className={field}
        >
          {GENERICS.map((g) => (
            <option key={g} value={g}>
              {GENERIC_LABEL[g]}
            </option>
          ))}
        </select>
      </label>
      <p className="text-xs text-ink-muted">
        The font must be installed on this device; until it is, the fallback is used.
      </p>
      {error !== null && (
        <p role="alert" className="text-sm font-semibold text-danger">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <button type="button" onClick={onDone} className={small}>
          Cancel
        </button>
        <button type="button" onClick={save} className={`${primary} flex-1`}>
          Save font
        </button>
      </div>
    </div>
  );
}

/**
 * The reader's settings: text size, line width (`wide`: the desktop layout, which also puts themes
 * and fonts side by side; the line width only matters from the desktop breakpoint up), colour theme and font, with editors for the user's own themes and fonts.
 */
export function ReaderSettings({
  reader,
  wide,
  onNewTheme,
}: {
  reader: Reader;
  wide: boolean;
  /** The New theme button was pressed; the owner shows `ThemeEditorDialog`. */
  onNewTheme: () => void;
}) {
  const { settings } = reader;
  const [editingFont, setEditingFont] = useState(false);
  const selectedTheme = reader.themes.find((theme) => theme.id === settings.themeId);
  const selectedFont = reader.fonts.find((font) => font.id === settings.fontId);
  return (
    <div
      role="group"
      aria-label="Reading settings"
      className={wide ? 'grid grid-cols-2 items-start gap-x-8 gap-y-5' : 'flex flex-col gap-4'}
    >
      <Section title="Text size">
        <div className="flex gap-2">
          {FONT_SIZES.map((size) => (
            <button
              key={size}
              type="button"
              aria-label={FONT_LABEL[size]}
              aria-pressed={settings.fontSize === size}
              onClick={() => {
                reader.setFontSize(size);
              }}
              className={`${choice(settings.fontSize === size)} flex-1 ${FONT_SAMPLE[size]}`}
            >
              A
            </button>
          ))}
        </div>
      </Section>
      {wide && (
        <Section title="Line width">
          <div className="flex gap-2">
            {WIDTHS.map((width) => (
              <button
                key={width}
                type="button"
                aria-pressed={settings.width === width}
                onClick={() => {
                  reader.setWidth(width);
                }}
                className={`${choice(settings.width === width)} flex-1 text-sm`}
              >
                {WIDTH_LABEL[width]}
              </button>
            ))}
          </div>
        </Section>
      )}
      <div role="group" aria-label="Theme" className="flex flex-col gap-2">
        <p className="text-sm font-semibold">Theme</p>
        <div className={GRID}>
          <Tile
            name="Match app"
            selected={settings.themeId === APP_THEME_ID}
            onPick={() => {
              reader.setTheme(APP_THEME_ID);
            }}
          />
          {reader.themes.map((theme) => (
            <Tile
              key={theme.id}
              name={theme.name}
              selected={settings.themeId === theme.id}
              onPick={() => {
                reader.setTheme(theme.id);
              }}
              sampleStyle={{ background: theme.page, color: theme.ink }}
            />
          ))}
          <AddTile label="New theme" onClick={onNewTheme} />
        </div>
        {selectedTheme?.id.startsWith(CUSTOM_ID_PREFIX) === true && (
          <button
            type="button"
            onClick={() => {
              reader.deleteTheme(selectedTheme.id);
            }}
            className={deleteButton}
          >
            {`Delete theme ${selectedTheme.name}`}
          </button>
        )}
      </div>
      <div role="group" aria-label="Font" className="flex flex-col gap-2">
        <p className="text-sm font-semibold">Font</p>
        <div className={GRID}>
          {reader.fonts.map((font) => (
            <Tile
              key={font.id}
              name={font.name}
              selected={settings.fontId === font.id}
              onPick={() => {
                reader.setFont(font.id);
              }}
              style={{ fontFamily: font.stack }}
            />
          ))}
          <AddTile
            label="Add font"
            onClick={() => {
              setEditingFont(true);
            }}
          />
        </div>
        {selectedFont?.id.startsWith(CUSTOM_ID_PREFIX) === true && (
          <button
            type="button"
            onClick={() => {
              reader.deleteFont(selectedFont.id);
            }}
            className={deleteButton}
          >
            {`Delete font ${selectedFont.name}`}
          </button>
        )}
        {editingFont && (
          <FontEditor
            reader={reader}
            onDone={() => {
              setEditingFont(false);
            }}
          />
        )}
      </div>
    </div>
  );
}
