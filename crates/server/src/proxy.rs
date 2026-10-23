//! Reverse proxy to the legacy Python dashboard, for every page the Rust server
//! has not ported yet. Bodies stream in both directions, so the chat's
//! server-sent events keep working through it.

use axum::body::Body;
use axum::http::{header, HeaderValue, Request, Response, StatusCode, Uri};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;

#[derive(Clone)]
pub struct Proxy {
    client: Client<HttpConnector, Body>,
    pub target_port: u16,
}

impl Proxy {
    pub fn new(target_port: u16) -> Self {
        Self {
            client: Client::builder(TokioExecutor::new()).build_http(),
            target_port,
        }
    }

    pub async fn forward(&self, mut req: Request<Body>) -> Response<Body> {
        let path_and_query = req
            .uri()
            .path_and_query()
            .map(|pq| pq.as_str().to_string())
            .unwrap_or_else(|| "/".to_string());
        let uri: Uri =
            match format!("http://127.0.0.1:{}{path_and_query}", self.target_port).parse() {
                Ok(u) => u,
                Err(_) => return status(StatusCode::BAD_REQUEST, "bad request target"),
            };
        *req.uri_mut() = uri;
        // The legacy server thinks it is serving its own port (CSRF origin checks).
        if let Ok(host) = HeaderValue::from_str(&format!("127.0.0.1:{}", self.target_port)) {
            req.headers_mut().insert(header::HOST, host);
        }
        match self.client.request(req).await {
            Ok(resp) => resp.map(Body::new),
            Err(e) => status(
                StatusCode::BAD_GATEWAY,
                &format!("the legacy dashboard did not answer: {e}"),
            ),
        }
    }
}

fn status(code: StatusCode, message: &str) -> Response<Body> {
    Response::builder()
        .status(code)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(Body::from(message.to_string()))
        .expect("static response")
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::routing::{get, post};
    use axum::Router;

    async fn upstream() -> u16 {
        let app = Router::new()
            .route("/hello", get(|| async { "hi from legacy" }))
            .route(
                "/echo",
                post(|body: String| async move { format!("got:{body}") }),
            )
            .route(
                "/host",
                get(|headers: axum::http::HeaderMap| async move {
                    headers["host"].to_str().unwrap().to_string()
                }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        port
    }

    async fn body_text(resp: Response<Body>) -> String {
        let bytes = http_body_util::BodyExt::collect(resp.into_body())
            .await
            .unwrap()
            .to_bytes();
        String::from_utf8(bytes.to_vec()).unwrap()
    }

    #[tokio::test]
    async fn forwards_get_post_and_rewrites_host() {
        let port = upstream().await;
        let proxy = Proxy::new(port);
        let r = proxy
            .forward(Request::get("/hello?x=1").body(Body::empty()).unwrap())
            .await;
        assert_eq!(r.status(), 200);
        assert_eq!(body_text(r).await, "hi from legacy");

        let r = proxy
            .forward(Request::post("/echo").body(Body::from("payload")).unwrap())
            .await;
        assert_eq!(body_text(r).await, "got:payload");

        let r = proxy
            .forward(
                Request::get("/host")
                    .header("host", "127.0.0.1:9999")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await;
        assert_eq!(body_text(r).await, format!("127.0.0.1:{port}"));
    }

    #[tokio::test]
    async fn a_dead_upstream_is_a_502_not_a_hang() {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let dead = l.local_addr().unwrap().port();
        drop(l);
        let r = Proxy::new(dead)
            .forward(Request::get("/x").body(Body::empty()).unwrap())
            .await;
        assert_eq!(r.status(), StatusCode::BAD_GATEWAY);
    }
}
