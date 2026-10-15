// The server writes the settings brand into the page (app/api/app.py index_page).
interface Window {
  APP_BRAND?: string;
}

// Props of page-local components that are not worth a named type.
type LooseProps = Record<string, any>;
