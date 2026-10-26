import './globals.css';

export const metadata = { title: 'PR Review Dashboard' };

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
